"""迁移执行凭证（迁移方案 §5.2 · R6-03 签名 token 设计）。

安全性质（设计原文口径）：
1. **载荷**：``batch_uid`` + ``executor_user_pk`` + ``authenticated_at`` + ``nonce``；
2. **签名**：``django.core.signing``（HMAC-SHA256）+ 专用 salt，与平台其余签名用途隔离。
   密钥服务端持有、不落日志不入库——载荷任何一字节被篡改即验签失败；
3. **有效期**：``max_age=900``（15 分钟 TTL），过期验签拒绝；
4. **单次使用**：nonce 消费制——验证通过即写入 AuditEvent（``billing.legacy_migration_auth``，
   nonce 列唯一约束），同一 nonce 第二次使用被 DB IntegrityError 拒绝（防截获重放）；
5. **绑定**：载荷 ``batch_uid`` == 目标批次、``executor_user_pk`` == ``batch.executed_by_id``、
   批次 status=ACTIVE。

服务级验证顺序严格按设计：**验签（含 TTL）→ nonce 消费 → 绑定校验 → scope 校验**。
执行身份只能来自签名凭证——不接受裸 User 对象、不接受自报 operator 参数。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from django.core import signing
from django.db import IntegrityError, transaction

CREDENTIAL_SALT = "legacy_migration.execution"
CREDENTIAL_MAX_AGE = 900

AUTH_AUDIT_ACTION = "billing.legacy_migration_auth"
DENIED_AUDIT_ACTION = "billing.legacy_migration_denied"

_REQUIRED_FIELDS = ("batch_uid", "executor_user_pk", "authenticated_at", "nonce")


class LegacyMigrationDenied(Exception):
    """受控入口拒绝执行（验签失败/重放/越范围/批次非法）。"""


@dataclass(frozen=True)
class ExecutionCredential:
    """验签通过的执行凭证载荷（执行身份的唯一权威来源）。"""

    batch_uid: str
    executor_user_pk: int
    authenticated_at: str
    nonce: str


def mint_credential(
    *,
    batch_uid: str,
    executor_user_pk: int,
    authenticated_at: datetime | None = None,
) -> str:
    """认证通过后由服务端铸造签名凭证（命令层是唯一铸造点）。"""
    stamp = authenticated_at or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    payload = {
        "batch_uid": str(batch_uid),
        "executor_user_pk": int(executor_user_pk),
        "authenticated_at": stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "nonce": uuid.uuid4().hex,
    }
    return signing.dumps(payload, salt=CREDENTIAL_SALT)


def load_credential(token: str) -> ExecutionCredential:
    """验签 + TTL 校验；任何异常都转成 LegacyMigrationDenied（不泄露密钥细节）。"""
    if not isinstance(token, str) or not token:
        raise LegacyMigrationDenied("缺少执行凭证；受控入口只接受服务端铸造的签名凭证")
    try:
        payload: Any = signing.loads(token, salt=CREDENTIAL_SALT, max_age=CREDENTIAL_MAX_AGE)
    except signing.SignatureExpired as exc:
        raise LegacyMigrationDenied(
            f"执行凭证已过期（TTL {CREDENTIAL_MAX_AGE} 秒），请重新进行密码认证"
        ) from exc
    except signing.BadSignature as exc:
        raise LegacyMigrationDenied("执行凭证验签失败：载荷被篡改或非本系统铸造") from exc
    if not isinstance(payload, dict):
        raise LegacyMigrationDenied("执行凭证载荷结构非法")
    missing = [field for field in _REQUIRED_FIELDS if field not in payload]
    if missing:
        raise LegacyMigrationDenied("执行凭证载荷缺少字段：" + ", ".join(missing))
    try:
        executor_pk = int(payload["executor_user_pk"])
    except (TypeError, ValueError) as exc:
        raise LegacyMigrationDenied("执行凭证载荷 executor_user_pk 非法") from exc
    return ExecutionCredential(
        batch_uid=str(payload["batch_uid"]),
        executor_user_pk=executor_pk,
        authenticated_at=str(payload["authenticated_at"]),
        nonce=str(payload["nonce"]),
    )


def _consume_nonce(credential: ExecutionCredential, *, batch: Any) -> None:
    """nonce 单次使用：写入 AuditEvent 唯一列，重复使用被数据库拒绝。

    审计事件模型禁止 update/delete，唯一约束即消费锁——不存在"先查再写"的竞态窗口。
    """
    from apps.core.models import AuditEvent

    try:
        with transaction.atomic():
            AuditEvent.objects.create(
                actor_id=credential.executor_user_pk,
                action=AUTH_AUDIT_ACTION,
                entity_type="LegacyMigrationBatch",
                entity_id=str(batch.pk),
                property=getattr(batch, "scope_property", None),
                reason=f"batch_uid={credential.batch_uid}",
                nonce=credential.nonce,
            )
    except IntegrityError as exc:
        raise LegacyMigrationDenied("执行凭证 nonce 已被使用：疑似重放攻击") from exc


def _check_binding(credential: ExecutionCredential, batch: Any) -> None:
    """用当前读锁定批次状态，避免过期对象绕过 ACTIVE/SoD 守卫。"""
    fresh = type(batch).objects.select_for_update().get(pk=batch.pk)
    if credential.batch_uid != fresh.batch_uid:
        raise LegacyMigrationDenied("凭证绑定的批次与目标批次不一致")
    if fresh.executed_by_id != credential.executor_user_pk:
        raise LegacyMigrationDenied("凭证执行人与批次登记的 executed_by 不一致")
    if fresh.status != fresh.Status.ACTIVE:
        raise LegacyMigrationDenied(f"批次状态为 {fresh.status}，仅 ACTIVE 批次可执行迁移")
    if fresh.approved_by_id is None:
        raise LegacyMigrationDenied("批次缺少正式批准人记录，禁止执行迁移")
    if fresh.approved_by_id == credential.executor_user_pk:
        raise LegacyMigrationDenied("批准人与执行人必须分离，禁止同一账号执行迁移")

    # 后续 scope 校验与写入使用锁定后的最新授权快照。
    for field in (
        "batch_uid",
        "executed_by_id",
        "approved_by_id",
        "status",
        "scope_property_id",
        "scope_source_models",
        "scope_record_range",
    ):
        setattr(batch, field, getattr(fresh, field))


def authorize_execution(token: str, *, batch: Any) -> ExecutionCredential:
    """受控入口的首道校验：验签 → nonce 消费 → 绑定校验（失败即 LegacyMigrationDenied）。"""
    credential = load_credential(token)
    _consume_nonce(credential, batch=batch)
    _check_binding(credential, batch)
    return credential


def _scope_range(batch: Any) -> dict[str, Any]:
    scope = batch.scope_record_range or {}
    if not isinstance(scope, dict):
        raise LegacyMigrationDenied("批次授权范围结构非法")
    return scope


def assert_record_in_scope(batch: Any, *, source_model: str, record_pk: int | str) -> None:
    """逐条校验记录 ⊆ batch.scope_record_range（越范围一律拒绝并审计）。"""
    allowed_models = batch.scope_source_models or []
    if source_model not in allowed_models:
        raise LegacyMigrationDenied(f"来源模型 {source_model} 不在批次授权范围 {allowed_models} 内")
    scope = _scope_range(batch)
    allowed_pks = scope.get("pks")
    if isinstance(allowed_pks, list):
        if str(record_pk) not in {str(pk) for pk in allowed_pks}:
            raise LegacyMigrationDenied(f"记录 {source_model}#{record_pk} 不在批次授权 id 清单内")
        return
    try:
        numeric_pk = int(record_pk)
    except (TypeError, ValueError) as exc:
        raise LegacyMigrationDenied(f"记录主键 {record_pk!r} 非法") from exc
    lower = scope.get("pk_min")
    upper = scope.get("pk_max")
    if lower is None or upper is None:
        raise LegacyMigrationDenied("批次授权范围缺少 pk_min/pk_max 或 pks 清单，无法执行")
    if not (int(lower) <= numeric_pk <= int(upper)):
        raise LegacyMigrationDenied(
            f"记录 {source_model}#{record_pk} 超出授权区间 [{lower}, {upper}]"
        )


def record_denial(*, batch: Any, reason: str, actor_id: int | None = None) -> None:
    """拒绝执行时落审计（含失败原因与尝试账号）；审计本身失败不掩盖原拒绝。"""
    from apps.core.models import AuditEvent

    try:
        with transaction.atomic():
            AuditEvent.objects.create(
                actor_id=actor_id,
                action=DENIED_AUDIT_ACTION,
                entity_type="LegacyMigrationBatch",
                entity_id=str(getattr(batch, "pk", "")),
                property=getattr(batch, "scope_property", None),
                reason=reason[:240],
            )
    except Exception:  # noqa: BLE001 - 审计写入失败不得把拒绝变成通过
        pass
