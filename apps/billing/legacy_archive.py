"""历史财务四表归档服务（归档门禁 §5 结构 + §4.2 checksum-v2 的落地实现）。

职责边界：
1. **只读旧表**（hotelapp 的 folio / finance_record / night_audit / accommodation），
   不写不改旧表——旧表处置属 D5/D6，本模块不越权；
2. **只写归档表**（legacy_*_archive）与批次元数据（LegacyArchiveRun）；
3. 归档行摘要与整表摘要全部由 :mod:`apps.billing.legacy_checksum` 在 Python 侧计算，
   不依赖任何 DB 方言函数（SQLite/MySQL 同数据必须同摘要）。

归档不可变：``LegacyArchiveRecord.save()`` 拒绝二次保存、QuerySet 拒绝批量改删。
同一批次重复归档同一张表一律拒绝（摘要登记后不可修改，重跑会污染对位关系）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from django.db import transaction

from apps.billing import models as billing_models
from apps.billing.legacy_checksum import build_metadata, row_digest, table_digest


class LegacyArchiveError(Exception):
    """归档受阻（批次重复、源表不可读、摘要不一致）。"""


@dataclass(frozen=True)
class ArchiveSpec:
    """一张旧表的归档契约：源模型 → 归档模型 + 列映射（列序即摘要顺序）。"""

    source_model: str
    source_table: str
    archive_table: str
    # 归档字段名 → 旧表字段名；元组顺序 = 归档表列序 = checksum-v2 编码顺序。
    column_map: tuple[tuple[str, str], ...]
    # PII 转换钩子（可选）：read_source_rows 读取后、写归档行前应用；
    # client/vip 用它做姓名/证件/联系方式的 Fernet 密文 + HMAC 盲索引
    # 转换（M5-4 §2.3 PII 门禁②）。
    transform: Any = None

    @property
    def columns(self) -> tuple[str, ...]:
        return tuple(archive_field for archive_field, _ in self.column_map)

    @property
    def archive_model(self) -> Any:
        return {
            "legacy_folio_archive": billing_models.LegacyFolioArchive,
            "legacy_finance_record_archive": billing_models.LegacyFinanceRecordArchive,
            "legacy_night_audit_archive": billing_models.LegacyNightAuditArchive,
            "legacy_accommodation_archive": billing_models.LegacyAccommodationArchive,
            "legacy_client_archive": billing_models.LegacyClientArchive,
            "legacy_vip_archive": billing_models.LegacyVipArchive,
            "legacy_staff_archive": billing_models.LegacyStaffArchive,
        }[self.archive_table]


def _encrypt_pii_pair(plain, normalize_fn, encrypt_fn, hash_fn):
    """(密文, 盲索引) 对：空值 → (None, None)；标准化后加密+HMAC。

    M5-4 §2.3：密文与盲索引共用 PII 独立密钥（与手机号/证件同源机制），
    不新造第二套加密实现。
    """
    normalized = normalize_fn(plain) if plain else None
    if not normalized:
        return None, None
    return encrypt_fn(normalized), hash_fn(normalized)


def _transform_client_row(row):
    """client 行 PII 转换：姓名/身份证号 → 密文+盲索引（id 明文保留）。

    明文键在产出归档行前弹出（V1.1 §3.2：明文不进归档表任何列）。
    """
    from apps.guests.name_pii import encrypt_guest_name, guest_name_hash, normalize_guest_name
    from apps.guests.services import doc_no_hash, encrypt_doc_no

    name_norm = normalize_guest_name(row.get("姓名") or "")
    row["name_ciphertext"] = encrypt_guest_name(name_norm) if name_norm else None
    row["name_hash"] = guest_name_hash(name_norm) if name_norm else None
    id_raw = (row.get("身份证号") or "").strip().upper()
    row["id_no_ciphertext"] = encrypt_doc_no(id_raw) if id_raw else None
    row["id_no_hash"] = doc_no_hash(id_raw) if id_raw else None
    row.pop("姓名", None)
    row.pop("身份证号", None)
    return row


def _transform_vip_row(row):
    """vip 行 PII 转换：姓名/联系方式 → 密文+盲索引（id/rank 明文保留）。"""
    from apps.core.phone import normalize_phone
    from apps.guests.name_pii import encrypt_guest_name, guest_name_hash, normalize_guest_name
    from apps.guests.services import encrypt_phone, phone_hash

    name_norm = normalize_guest_name(row.get("姓名") or "")
    row["name_ciphertext"] = encrypt_guest_name(name_norm) if name_norm else None
    row["name_hash"] = guest_name_hash(name_norm) if name_norm else None
    contact_norm = normalize_phone(row.get("联系方式") or "")
    row["contact_ciphertext"] = encrypt_phone(contact_norm) if contact_norm else None
    row["contact_hash"] = phone_hash(contact_norm) if contact_norm else None
    row["rank_text"] = row.get("会员等级") or ""
    row.pop("姓名", None)
    row.pop("联系方式", None)
    row.pop("会员等级", None)
    return row


def _transform_staff_row(row):
    """staff 行 PII 转换：姓名 → 密文+盲索引（职务/薪酬/工龄/启用明文保留）。

    ARCH-04 C8：旧员工表的归档落点。姓名属 PII（V1.1 §3.2 明文快照已否决），
    其余列为非标识历史事实（薪酬/工龄的归属仍待负责人裁定 C12），
    本归档只做旧表全列快照，不向新业务模型迁移任何字段。
    """
    from apps.guests.name_pii import encrypt_guest_name, guest_name_hash, normalize_guest_name

    name_norm = normalize_guest_name(row.get("姓名") or "")
    row["name_ciphertext"] = encrypt_guest_name(name_norm) if name_norm else None
    row["name_hash"] = guest_name_hash(name_norm) if name_norm else None
    row["job_title_text"] = row.get("职务") or ""
    row["salary_text"] = row.get("月薪") or ""
    row["service_years_text"] = row.get("工龄") or ""
    row["enabled_text"] = "启用" if row.get("启用") else "停用"
    row.pop("姓名", None)
    row.pop("职务", None)
    row.pop("月薪", None)
    row.pop("工龄", None)
    row.pop("启用", None)
    return row


ARCHIVE_SPECS: tuple[ArchiveSpec, ...] = (
    ArchiveSpec(
        source_model="Folio",
        source_table="folio",
        archive_table="legacy_folio_archive",
        column_map=(
            ("id", "id"),
            ("order_id", "order_id"),
            ("entry_type", "类型"),
            ("amount_text", "金额"),
            ("subject", "事由"),
            ("date_text", "日期"),
            ("handler", "经手人"),
        ),
    ),
    ArchiveSpec(
        source_model="FinanceRecord",
        source_table="finance_record",
        archive_table="legacy_finance_record_archive",
        column_map=(
            ("id", "id"),
            ("record_type", "类型"),
            ("amount_text", "金额"),
            ("subject", "事由"),
            ("related_order", "关联订单"),
            ("date_text", "日期"),
            ("handler", "经手人"),
        ),
    ),
    ArchiveSpec(
        source_model="NightAudit",
        source_table="night_audit",
        archive_table="legacy_night_audit_archive",
        column_map=(
            ("id", "id"),
            ("business_date_text", "营业日期"),
            ("in_house_count", "在住单数"),
            ("rollover_total_text", "过租总额"),
            ("no_show_count", "未到处理数"),
            ("operator", "操作人"),
            ("executed_at_text", "执行时间"),
        ),
    ),
    ArchiveSpec(
        source_model="Accommodation",
        source_table="accommodation",
        archive_table="legacy_accommodation_archive",
        column_map=(
            ("id", "id"),
            ("client_id", "顾客id"),
            ("room_id", "客房编号"),
            ("check_in_text", "入住时间"),
            ("status", "状态"),
            ("contact_phone", "联系电话"),
            ("received_amount_text", "收款金额"),
            ("deposit_text", "押金"),
            ("check_out_text", "退房时间"),
            ("reversed_by", "冲账人"),
            ("reversed_at_text", "冲账时间"),
        ),
    ),
    # M5-4 §2.1：client/vip 归档 spec（PII 密文+盲索引门禁版，V1.1 §3.2 裁定）。
    # 归档字段 → 源明文列；PII 列源值仅经 transform 加密后落密文+盲索引双列。
    ArchiveSpec(
        source_model="Client",
        source_table="client",
        archive_table="legacy_client_archive",
        column_map=(
            ("id", "顾客id"),
            ("name_ciphertext", "姓名"),
            ("name_hash", "姓名"),
            ("id_no_ciphertext", "身份证号"),
            ("id_no_hash", "身份证号"),
        ),
        transform=_transform_client_row,
    ),
    ArchiveSpec(
        source_model="Vip",
        source_table="vip",
        archive_table="legacy_vip_archive",
        column_map=(
            ("id", "会员编号"),
            ("name_ciphertext", "姓名"),
            ("name_hash", "姓名"),
            ("rank_text", "会员等级"),
            ("contact_ciphertext", "联系方式"),
            ("contact_hash", "联系方式"),
        ),
        transform=_transform_vip_row,
    ),
    # ARCH-04 C8：旧 staff 表归档 spec（员工主档；姓名 PII 密文+盲索引）。
    # 旧表只读历史档案，本 spec 只做全列快照，不写旧表、不改旧表。
    ArchiveSpec(
        source_model="Staff",
        source_table="staff",
        archive_table="legacy_staff_archive",
        column_map=(
            ("id", "员工id"),
            ("name_ciphertext", "姓名"),
            ("name_hash", "姓名"),
            ("job_title_text", "职务"),
            ("salary_text", "月薪"),
            ("service_years_text", "工龄"),
            ("enabled_text", "启用"),
        ),
        transform=_transform_staff_row,
    ),
)

_BY_MODEL = {spec.source_model: spec for spec in ARCHIVE_SPECS}


def get_spec(source_model: str) -> ArchiveSpec:
    try:
        return _BY_MODEL[source_model]
    except KeyError as exc:
        raise LegacyArchiveError(
            f"未知归档来源模型 {source_model!r}；可选：{', '.join(sorted(_BY_MODEL))}"
        ) from exc


def _source_queryset(spec: ArchiveSpec) -> Any:
    """延迟导入旧表模型：避免 apps 在 app registry 就绪前耦合 hotelapp。"""
    from hotelapp import models as legacy_models

    model = {
        "Folio": legacy_models.Folio,
        "FinanceRecord": legacy_models.FinanceRecord,
        "NightAudit": legacy_models.NightAudit,
        "Accommodation": legacy_models.Accommodation,
        "Client": legacy_models.Client,
        "Vip": legacy_models.Vip,
        "Staff": legacy_models.Staff,
    }[spec.source_model]
    return model.objects.all()


def read_source_rows(spec: ArchiveSpec) -> list[dict[str, Any]]:
    """按归档列序读取旧表（只读）；返回的键为**归档字段名**。

    带 transform 的 spec（client/vip PII 门禁）：column_map 的源列是
    **原始明文列**，读取后由 transform 整行重写为归档字段（密文+盲索引），
    源明文不出现在返回行里。
    """
    if spec.transform is not None:
        # 主键 = column_map 首对的源列（client="顾客id"、vip="会员编号"；
        # 旧表主键列名非 "id"），归档行主键沿用源表主键。
        pk_source = spec.column_map[0][1]
        pk_archive = spec.column_map[0][0]
        allowed = {af for af, _ in spec.column_map}
        source_fields = sorted({sf for _, sf in spec.column_map})
        rows: list[dict[str, Any]] = []
        for raw in _source_queryset(spec).values(*source_fields).order_by(pk_source):
            row = spec.transform(raw)
            row[pk_archive] = raw[pk_source]
            # 只保留归档模型字段（弹出残留的中文源列键）
            rows.append({k: v for k, v in row.items() if k in allowed})
        return rows
    source_fields = [source_field for _, source_field in spec.column_map]
    rows = []
    for raw in _source_queryset(spec).values(*source_fields).order_by("id"):
        rows.append(
            {archive_field: raw[source_field] for archive_field, source_field in spec.column_map}
        )
    return rows


def summarize_source(spec: ArchiveSpec) -> dict[str, Any]:
    """只读盘点（dry-run 用）：行数 + min/max 主键，不写任何表。"""
    rows = read_source_rows(spec)
    return {
        "source_model": spec.source_model,
        "source_table": spec.source_table,
        "archive_table": spec.archive_table,
        "row_count": len(rows),
        "min_pk": min((row["id"] for row in rows), default=None),
        "max_pk": max((row["id"] for row in rows), default=None),
    }


def archive_table(*, spec: ArchiveSpec, batch_uid: str, operator: str) -> Any:
    """归档一张旧表：逐行写归档行（含行摘要）+ 登记整表摘要元数据。

    幂等口径：同一 (batch_uid, source_table) 只允许归档一次——重跑直接拒绝，
    不静默覆盖（归档事实不可变，覆盖会破坏"归档行=源表快照"的对位关系）。
    """
    batch_uid = str(batch_uid or "").strip()
    operator = str(operator or "").strip()
    if not batch_uid or len(batch_uid) > 64:
        raise LegacyArchiveError("归档批次号必须为 1 至 64 个字符。")
    if not operator:
        raise LegacyArchiveError("归档执行人不能为空（元数据须登记执行人）。")
    if billing_models.LegacyArchiveRun.objects.filter(
        batch_uid=batch_uid, source_table=spec.source_table
    ).exists():
        raise LegacyArchiveError(
            f"批次 {batch_uid} 已归档 {spec.source_table}；归档事实不可变，禁止重跑覆盖。"
        )

    columns = spec.columns
    rows = read_source_rows(spec)
    digests = [row_digest(row, columns).hex() for row in rows]
    summary = table_digest(rows, columns, "id")
    meta = build_metadata(
        source_table=spec.archive_table,
        rows=rows,
        pk_field="id",
        table_summary=summary,
        computed_at=datetime.now(timezone.utc),
        operator=operator,
    )
    archive_model = spec.archive_model
    # FIN-02 裁定：归档行落物业归属快照。来源只能是已登记的迁移批次授权范围，
    # 不从 room_id 反推（同号多物业历史数据会静默错归）；未登记批次保持 NULL，
    # 由具备权限的人员按裁定 3 逐记录核验后再回填。
    batch = billing_models.LegacyMigrationBatch.objects.filter(batch_uid=batch_uid).first()
    property_id = batch.scope_property_id if batch is not None else None

    with transaction.atomic():
        for row, digest in zip(rows, digests):
            archive_model(
                **row,
                archive_batch=batch_uid,
                verified_checksum=digest,
                property_id=property_id,
            ).save()
        run = billing_models.LegacyArchiveRun.objects.create(
            batch_uid=batch_uid,
            source_table=spec.source_table,
            archive_table=spec.archive_table,
            algorithm_version=meta["algorithm_version"],
            row_count=meta["row_count"],
            min_pk=meta["min_pk"],
            max_pk=meta["max_pk"],
            table_digest=meta["table_digest"],
            computed_at=datetime.strptime(meta["computed_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            ),
            operator=operator,
        )
    return run


def verify_archive_run(run: Any) -> dict[str, Any]:
    """D4 恢复演练/收口核验：从归档表重算摘要与行数，与登记元数据比对。"""
    spec = get_spec_by_table(run.source_table)
    columns = spec.columns
    rows = list(
        spec.archive_model.objects.filter(archive_batch=run.batch_uid)
        .values(*columns)
        .order_by("id")
    )
    # 空表也要算：table_digest(()) = SHA-256(空串)，与归档时同一算法，缺省成 None 会误报不一致。
    digest = table_digest(rows, columns, "id")
    ok = digest == run.table_digest and len(rows) == run.row_count
    return {
        "batch_uid": run.batch_uid,
        "source_table": run.source_table,
        "expected_rows": run.row_count,
        "actual_rows": len(rows),
        "expected_digest": run.table_digest,
        "actual_digest": digest,
        "ok": ok,
    }


def get_spec_by_table(source_table: str) -> ArchiveSpec:
    for spec in ARCHIVE_SPECS:
        if spec.source_table == source_table:
            return spec
    raise LegacyArchiveError(f"未知源表 {source_table!r}")


def archive_manifest(batch_uid: str) -> Sequence[Any]:
    return list(
        billing_models.LegacyArchiveRun.objects.filter(batch_uid=batch_uid).order_by("source_table")
    )
