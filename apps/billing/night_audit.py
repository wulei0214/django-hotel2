"""PMS2 夜审领域服务：只对 PMS2 Stay/Folio/Ledger 写入，不读写旧 FinanceRecord。"""
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from apps.billing.models import ApprovalRecord, Folio, LedgerEntry
from apps.billing.services_v2 import BillingService
from apps.core.models import (
    AuditEvent,
    BreakGlassRecord,
    BusinessDate,
    NightAuditRun,
    Property,
)
from apps.reservations.models import Reservation, ReservationRoom, Stay


class NightAuditError(Exception):
    """夜审业务校验失败。"""


# MAINT-02：夜审 RUNNING 记录在主过账事务外先提交，进程被 OOM/断电硬杀时
# 不会走 run() 的 except 分支，RUNNING 会永久残留，导致该营业日再也无法
# 执行夜审。超时阈值用于惰性恢复与运维恢复命令。
NIGHT_AUDIT_RUN_TIMEOUT_MINUTES = 15


def recover_stale_night_audit_runs(*, property_obj=None, business_date=None,
                                   timeout_minutes=NIGHT_AUDIT_RUN_TIMEOUT_MINUTES):
    """把超过阈值的 RUNNING 夜审标记为 FAILED，返回恢复条数。

    只处理明确超时的记录：正在执行的夜审在阈值内不受影响。调用方应处于
    事务内（run() 的并发仲裁块已满足；管理命令自行包 atomic）。
    """
    cutoff = timezone.now() - timedelta(minutes=timeout_minutes)
    qs = NightAuditRun.objects.filter(
        status=NightAuditRun.RunStatus.RUNNING, started_at__lt=cutoff)
    if property_obj is not None:
        qs = qs.filter(business_date__property=property_obj)
    if business_date is not None:
        qs = qs.filter(business_date=business_date)
    stale = list(qs.values_list("pk", "business_date__property_id"))
    if not stale:
        return 0
    NightAuditRun.objects.filter(pk__in=[pk for pk, _ in stale]).update(
        status=NightAuditRun.RunStatus.FAILED,
        finished_at=timezone.now(),
        error_message=(f"夜审执行超过 {timeout_minutes} 分钟未结束，进程可能已中断；"
                       "由超时恢复流程标记为失败，可重新发起夜审。"))
    props = {p.pk: p for p in Property.objects.filter(
        pk__in={prop_id for _, prop_id in stale})}
    for pk, prop_id in stale:
        AuditEvent.record(
            action="night_audit.run_recovered", entity_type="NightAuditRun",
            entity_id=pk, property_obj=props.get(prop_id),
            after={"status": "FAILED", "timeout_minutes": timeout_minutes},
            reason="RUNNING 超时恢复（MAINT-02）")
    return len(stale)


def property_date(property_obj):
    try:
        tz = ZoneInfo(property_obj.timezone or "Asia/Shanghai")
    except Exception:
        tz = ZoneInfo("Asia/Shanghai")
    return timezone.localdate(timezone.now(), tz)


class NightAuditService:
    """营业日过租服务；每条 Stay 每个营业日凭证使用固定 reference 实现幂等。"""

    @staticmethod
    def _resolve_date(property_obj, business_date=None):
        """解析并校验夜审目标营业日（WB-080）。

        只允许与"当前物业唯一 OPEN/REOPENED 营业日"一致的日期：
          * 已有 OPEN/REOPENED 记录 → 目标日期必须等于该记录的营业日；
          * 无 OPEN/REOPENED 记录（首夜审/全部已关账）→ 只允许物业当前
            营业日（此时 run() 才会 get_or_create 建立当日记录）。
        未来日期、历史日期、无记录日期一律拒绝，不再自动创建 BusinessDate。
        若未来确有"补审历史营业日"需求，必须先设计独立授权入口（走
        营业日重开审批）与快照重算机制，当前按需求登记 TODO 不实现。
        """
        if business_date is None:
            day = property_date(property_obj)
        else:
            if isinstance(business_date, str):
                try:
                    day = date.fromisoformat(business_date)
                except ValueError as exc:
                    raise NightAuditError("营业日期必须是 YYYY-MM-DD。") from exc
            elif isinstance(business_date, date):
                day = business_date
            else:
                raise NightAuditError("营业日期无效。")
        current = BusinessDate.objects.filter(
            property=property_obj,
            status__in=(BusinessDate.Status.OPEN, BusinessDate.Status.REOPENED),
        ).order_by("-business_date").first()
        if current is not None:
            if day != current.business_date:
                raise NightAuditError(
                    f"只允许审计当前营业日：物业当前的 {current.status} 营业日是 "
                    f"{current.business_date.isoformat()}，日期 "
                    f"{day.isoformat()} 与其不符，拒绝夜审；"
                    "补记历史凭证请走营业日重开授权流程。")
            return day
        today = property_date(property_obj)
        if day != today:
            raise NightAuditError(
                f"只允许审计当前营业日 {today.isoformat()}；日期 "
                f"{day.isoformat()} 无对应的 OPEN/REOPENED 营业日记录，拒绝夜审。")
        return day

    @classmethod
    def preview(cls, *, property_obj, business_date=None):
        day = cls._resolve_date(property_obj, business_date)
        stays = list(
            Stay.objects.filter(property=property_obj, status=Stay.Status.IN_HOUSE)
            .select_related("reservation_room", "reservation_room__reservation",
                            "reservation_room__rate_plan", "room")
            .order_by("id")
        )
        rows = []
        blockers = []
        # PERF-01：批量预取主账单与既有夜审凭证，替代循环内逐 Stay 两条查询
        # （N 家在住 = 2N+1 条查询 → 固定 3 条）。语义与原 .first() 一致：
        # 同预订多张 OPEN 主账单属异常态，setdefault 保留默认排序下的第一条
        # （Folio Meta ordering=-opened_at，即最近开立的账单）。
        window_reservation_ids = {
            stay.reservation_room.reservation_id for stay in stays
            if stay.reservation_room.check_in <= day < stay.reservation_room.check_out
        }
        folios_by_reservation = {}
        if window_reservation_ids:
            for folio_row in Folio.objects.filter(
                property=property_obj,
                reservation_id__in=window_reservation_ids,
                folio_type=Folio.FolioType.MASTER,
                status=Folio.Status.OPEN,
            ).only("id", "reservation_id"):
                folios_by_reservation.setdefault(
                    folio_row.reservation_id, folio_row)
        window_references = [
            f"night-audit:{property_obj.pk}:{day.isoformat()}:stay:{stay.pk}"
            for stay in stays
            if stay.reservation_room.check_in <= day < stay.reservation_room.check_out
        ]
        entries_by_reference = {}
        if window_references:
            for entry_row in LedgerEntry.objects.filter(
                property=property_obj, reference__in=window_references,
            ).only("reference", "folio_id", "stay_id", "entry_type",
                   "amount", "currency", "business_date"):
                entries_by_reference.setdefault(entry_row.reference, entry_row)
        total = Decimal("0.00")
        # WB-081：stay_count 与过租过滤同口径——只统计房价窗口覆盖目标
        # 营业日（check_in <= day < check_out）的在住 Stay。
        stay_count = 0
        for stay in stays:
            line = stay.reservation_room
            booking_no = line.reservation.booking_no
            # 房价窗口守卫：营业日不在 [check_in, check_out) 内的入住单
            # 不属当晚应过租范围，也不得因窗口外缺快照而阻断夜审
            # （用户报告：legacy 在住数据 check_in 晚于营业日被误判缺快照）。
            # 与下方 no-show 候选查询的 check_in__lte=day / check_out__gt=day 同口径。
            if not (line.check_in <= day < line.check_out):
                continue
            stay_count += 1
            nightly = next(
                (item for item in (line.nightly_rates or [])
                 if str(item.get("date")) == day.isoformat()),
                None,
            )
            if nightly is None:
                blockers.append({
                    "code": "NIGHTLY_RATE_MISSING",
                    "severity": "critical",
                    "entity": {"type": "Stay", "id": stay.pk,
                               "booking_no": booking_no},
                    "message": (f"预订 {booking_no} 的房间明细 {line.pk} "
                                f"缺少 {day.isoformat()} 的每晚房价快照。"),
                    "suggested_action": "核对预订房价快照；不得在缺少价格依据时自动过租。",
                })
                continue
            try:
                amount = Decimal(str(nightly.get("price")))
            except (InvalidOperation, TypeError, ValueError):
                blockers.append({
                    "code": "NIGHTLY_RATE_INVALID",
                    "severity": "critical",
                    "entity": {"type": "Stay", "id": stay.pk,
                               "booking_no": booking_no},
                    "message": f"预订 {booking_no} 的当日房价快照不是有效金额。",
                    "suggested_action": "修复并核验原始房价快照后重新预览。",
                })
                continue
            try:
                valid_amount = (amount.is_finite() and amount >= 0
                                and amount == amount.quantize(Decimal("0.01")))
            except InvalidOperation:
                valid_amount = False
            if not valid_amount:
                blockers.append({
                    "code": "NIGHTLY_RATE_INVALID",
                    "severity": "critical",
                    "entity": {"type": "Stay", "id": stay.pk,
                               "booking_no": booking_no},
                    "message": f"预订 {booking_no} 的当日房价金额无效。",
                    "suggested_action": "修复并核验原始房价快照后重新预览。",
                })
                continue
            folio = folios_by_reservation.get(line.reservation_id)
            if folio is None:
                blockers.append({
                    "code": "MASTER_FOLIO_MISSING",
                    "severity": "critical",
                    "entity": {"type": "Reservation", "id": line.reservation_id,
                               "booking_no": booking_no},
                    "message": f"预订 {booking_no} 缺少有效的未结主账单。",
                    "suggested_action": "核查账单归属与状态；不得自动创建重复账单。",
                })
                continue
            reference = f"night-audit:{property_obj.pk}:{day.isoformat()}:stay:{stay.pk}"
            # WB-082：already_posted 从"同物业同 reference 存在"升级为内容一致：
            # 同 folio、stay 关联（reference 内嵌 stay id 即本 stay，由
            # entry.stay_id == stay.pk 校验兜底）、ROOM_CHARGE、金额等于当晚
            # 房价、币种、营业日等于目标日。任一不符记为账务冲突 blocker，
            # 不得按"已过租"跳过。执行阶段 post_entry 的 _entry_facts_match
            # 失败路径保持不变（同一事实口径兜底并发窗口）。
            existing_entry = entries_by_reference.get(reference)
            already_posted = False
            if existing_entry is not None:
                differences = []
                if existing_entry.folio_id != folio.pk:
                    differences.append(
                        f"主账单（账本 folio {existing_entry.folio_id} ≠ "
                        f"本次 folio {folio.pk}）")
                if existing_entry.stay_id != stay.pk:
                    differences.append(
                        f"关联住客（账本 stay {existing_entry.stay_id} ≠ "
                        f"本次 stay {stay.pk}）")
                if existing_entry.entry_type != LedgerEntry.EntryType.ROOM_CHARGE:
                    differences.append(
                        f"凭证类型（账本 {existing_entry.entry_type} ≠ "
                        f"{LedgerEntry.EntryType.ROOM_CHARGE}）")
                if existing_entry.amount != amount:
                    differences.append(
                        f"金额（账本 {existing_entry.amount} ≠ 当晚房价 {amount}）")
                if existing_entry.currency != line.reservation.currency:
                    differences.append(
                        f"币种（账本 {existing_entry.currency} ≠ "
                        f"{line.reservation.currency}）")
                if existing_entry.business_date != day:
                    differences.append(
                        f"营业日（账本 {existing_entry.business_date.isoformat()} ≠ "
                        f"目标日 {day.isoformat()}）")
                if differences:
                    blockers.append({
                        "code": "ALREADY_POSTED_CONFLICT",
                        "severity": "critical",
                        "entity": {"type": "Stay", "id": stay.pk,
                                   "booking_no": booking_no},
                        "message": (
                            f"预订 {booking_no} 已存在同引用凭证 {reference}，"
                            f"但账务事实不一致（{'；'.join(differences)}），"
                            "不得跳过过租，须人工核销错账后重审。"),
                        "suggested_action": (
                            "核对账本凭证与房价快照；错账必须走红字冲销"
                            "（需审批），禁止直接修改或删除凭证。"),
                    })
                    continue
                already_posted = True
            rows.append({
                "stay_id": stay.pk,
                "booking_no": booking_no,
                "room_number": stay.room.number,
                "amount": str(amount),
                "currency": line.reservation.currency,
                "folio_id": folio.pk,
                "already_posted": already_posted,
                "reference": reference,
            })
            if not already_posted:
                total += amount

        candidates = ReservationRoom.objects.filter(
            property=property_obj,
            check_in__lte=day,
            check_out__gt=day,
            reservation__status__in=(Reservation.Status.CONFIRMED,
                                     Reservation.Status.CHECKED_IN),
            stay__isnull=True,
            no_show_record__isnull=True,
        ).count()
        bd = BusinessDate.objects.filter(
            property=property_obj, business_date=day
        ).first()
        return {
            "business_date": day.isoformat(),
            "business_date_status": bd.status if bd else BusinessDate.Status.OPEN,
            "stays": rows,
            "stay_count": stay_count,
            "charge_count": sum(1 for row in rows if not row["already_posted"]),
            "already_posted_count": sum(1 for row in rows if row["already_posted"]),
            "charge_total": str(total.quantize(Decimal("0.01"))),
            "currency": property_obj.currency,
            "no_show_review_candidates": candidates,
            "blockers": blockers,
            "blocker_count": len(blockers),
            "can_run": not blockers,
            "note": "未到候选只列示，不在夜审中自动标记或没收押金；需按已披露政策由授权员工处理。",
        }

    @classmethod
    def run(cls, *, property_obj, executed_by, business_date=None):
        day = cls._resolve_date(property_obj, business_date)

        # 先建立 RUNNING 记录并串行化同一物业的并发夜审。
        with transaction.atomic():
            Property.objects.select_for_update().get(pk=property_obj.pk)
            bd, _ = BusinessDate.objects.get_or_create(
                property=property_obj, business_date=day,
                defaults={"status": BusinessDate.Status.OPEN},
            )
            bd = BusinessDate.objects.select_for_update().get(pk=bd.pk)
            successful = NightAuditRun.objects.filter(
                business_date=bd, status=NightAuditRun.RunStatus.SUCCESS
            ).order_by("-started_at").first()
            if bd.status == BusinessDate.Status.CLOSED and successful:
                return successful, False
            if bd.status == BusinessDate.Status.CLOSED:
                raise NightAuditError("该营业日已关账但没有成功夜审记录，请联系管理员核查。")
            if bd.status not in (BusinessDate.Status.OPEN, BusinessDate.Status.REOPENED):
                raise NightAuditError("当前营业日状态不允许执行夜审。")
            other_open = BusinessDate.objects.filter(
                property=property_obj,
                status__in=(BusinessDate.Status.OPEN, BusinessDate.Status.REOPENED),
            ).exclude(pk=bd.pk).first()
            if other_open:
                raise NightAuditError(
                    f"物业已有未关闭营业日 {other_open.business_date.isoformat()}，请先处理。"
                )
            # MAINT-02：惰性恢复。进程被硬杀时 except 分支不会执行，
            # 残留 RUNNING 会让该营业日永久无法再跑夜审。
            recover_stale_night_audit_runs(
                property_obj=property_obj, business_date=bd)
            running = NightAuditRun.objects.filter(
                business_date=bd, status=NightAuditRun.RunStatus.RUNNING
            ).first()
            if running:
                raise NightAuditError("该营业日已有夜审正在执行，请勿重复提交。")
            run = NightAuditRun.objects.create(
                business_date=bd, status=NightAuditRun.RunStatus.RUNNING,
                executed_by=executed_by,
            )

        try:
            with transaction.atomic():
                # 收尾并发专项（MySQL 1213 死锁修复，锁序重排）：过账事务的
                # Stay 行锁必须先于 Property/BusinessDate 行锁获取。原因：
                # 退房事务持有 Stay 行锁后插入 LedgerEntry/审计事件，InnoDB
                # 外键校验会对父行 core_property 取共享锁；若夜审先持有
                # Property 排他锁再等 Stay 行锁，即形成
                # 夜审{property X → 等 stay} vs 退房{stay X → 等 property S}
                # 的 ABBA 死锁（MySQL 1213）。Stay 先锁后，并发退房要么在
                # Stay 行锁上等待（未持有任何夜审所需锁），要么已提交
                # （Stay 不再 IN_HOUSE，被过滤跳过），无环可成。
                candidate_stay_pks = list(
                    Stay.objects.filter(
                        property=property_obj,
                        status=Stay.Status.IN_HOUSE,
                        reservation_room__check_in__lte=day,
                        reservation_room__check_out__gt=day,
                    ).values_list("pk", flat=True)
                )
                # WB-109：夜审与退房并发仲裁。在写入任何房费之前，先按主键序
                # 对本次涉及的全部 Stay（IN_HOUSE 且住宿区间覆盖目标营业日）
                # select_for_update()；锁序 Stay→ReservationRoom→Reservation
                # →Folio 与 check_out_stay 一致，杜绝 ABBA 死锁窗口：
                #   * 夜审先锁 → 并发退房在 Stay 行锁上等待，夜审提交后退房
                #     按"已过租差额"口径结算，只收一次房费；
                #   * 退房先锁/已提交 → Stay 不再 IN_HOUSE，夜审跳过该 Stay，
                #     房费由退房事务结算，夜审不重复计费。
                # （收尾并发专项补充：锁定读只锁 Stay 行——区间条件用前置
                # 一致性读求候选主键超集，再以纯 Stay 单表 select_for_update
                # 锁定，避免 JOIN 锁定读把 ReservationRoom 行也一并锁住、
                # 锁序退化为 RR→Stay；status=IN_HOUSE 留在锁定语句内，锁
                # 等待结束后按当前读重评（锁定后已退房的 Stay 仍被过滤），
                # WB-109 语义不变；住宿区间并发变化由过账循环内锁定 RR 后
                # 的窗口复核兜底。）
                locked_stays = {
                    row.pk: row
                    for row in Stay.objects.select_for_update().filter(
                        pk__in=candidate_stay_pks,
                        property=property_obj,
                        status=Stay.Status.IN_HOUSE,
                    ).order_by("pk")
                }
                Property.objects.select_for_update().get(pk=property_obj.pk)
                bd = BusinessDate.objects.select_for_update().get(pk=bd.pk)
                if bd.status not in (BusinessDate.Status.OPEN, BusinessDate.Status.REOPENED):
                    raise NightAuditError("营业日状态已变化，夜审未执行。")
                preview = cls.preview(property_obj=property_obj, business_date=day)
                if preview.get("blockers"):
                    codes = sorted({item.get("code", "UNKNOWN")
                                     for item in preview["blockers"]})
                    raise NightAuditError(
                        "夜审存在 %s 项未解决阻断，执行已停止：%s" %
                        (len(preview["blockers"]), "、".join(codes)))
                posted = 0
                already = 0
                summary_stay_count = 0
                summary_charge_total = Decimal("0.00")
                for row in preview["stays"]:
                    stay = locked_stays.get(row["stay_id"])
                    if stay is None:
                        # WB-109：锁定后退房并发获胜（Stay 已退房/不在锁定集）
                        # → 夜审跳过该 Stay，不写凭证、不计数。
                        continue
                    summary_stay_count += 1
                    if row["already_posted"]:
                        already += 1
                        continue
                    line = ReservationRoom.objects.select_for_update().get(
                        pk=stay.reservation_room_id, property=property_obj)
                    if not (line.check_in <= day < line.check_out):
                        # 锁定后住宿区间不再覆盖营业日（如并发续住/缩短）→ 跳过。
                        continue
                    reservation = Reservation.objects.select_for_update().get(
                        pk=line.reservation_id, property=property_obj)
                    # 锁定行上重读当晚房价快照（预览读发生在加锁前，金额以
                    # 锁定事实为准；缺失/非法按既有 blocker 口径整体回滚）。
                    nightly = next(
                        (item for item in (line.nightly_rates or [])
                         if str(item.get("date")) == day.isoformat()),
                        None,
                    )
                    if nightly is None:
                        raise NightAuditError(
                            f"预订 {reservation.booking_no} 的房间明细 {line.pk} "
                            f"缺少 {day.isoformat()} 的每晚房价快照，夜审已整体回滚。"
                        )
                    try:
                        amount = Decimal(str(nightly.get("price")))
                    except (InvalidOperation, TypeError, ValueError) as exc:
                        raise NightAuditError(
                            f"预订 {reservation.booking_no} 的当日房价快照不是"
                            "有效金额，夜审已整体回滚。") from exc
                    folio = Folio.objects.select_for_update().filter(
                        property=property_obj,
                        reservation__booking_no=reservation.booking_no,
                        folio_type=Folio.FolioType.MASTER,
                        status=Folio.Status.OPEN,
                    ).first()
                    if folio is None:
                        raise NightAuditError(
                            f"预订 {reservation.booking_no} 没有有效主账单，夜审已整体回滚。"
                        )
                    if amount == 0:
                        # 零价房不创建零金额凭证，但保留在结果汇总。
                        already += 1
                        continue
                    reference = (
                        f"night-audit:{property_obj.pk}:{day.isoformat()}"
                        f":stay:{stay.pk}")
                    # WB-079 顺序不变量：过租凭证写入必须发生在营业日置
                    # CLOSED 之前（同一事务内，先 post_entry 后关账）——
                    # post_entry 的营业日关账守卫依赖该顺序，否则会阻断
                    # 合法夜审链路。关账后本事务内不再有任何账本写入。
                    BillingService.post_entry(
                        property_obj=property_obj, folio=folio, stay=stay,
                        entry_type=LedgerEntry.EntryType.ROOM_CHARGE,
                        amount=amount, currency=reservation.currency,
                        business_date=day, reference=reference,
                        description=f"夜审房费过租 {day.isoformat()}",
                        posted_by=executed_by,
                    )
                    posted += 1
                    summary_charge_total += amount

                now = timezone.now()
                bd.status = BusinessDate.Status.CLOSED
                bd.closed_at = now
                bd.closed_by = executed_by
                bd.save(update_fields=("status", "closed_at", "closed_by", "open_property_key"))
                summary = {
                    # WB-109：汇总按锁定后实际处理的 Stay 口径重算，
                    # 与 preview（无锁读）口径在无并发时一致。
                    "stay_count": summary_stay_count,
                    "charge_count": posted,
                    "already_posted_count": already,
                    "charge_total": str(summary_charge_total.quantize(
                        Decimal("0.01"))),
                    "currency": preview["currency"],
                    "no_show_review_candidates": preview["no_show_review_candidates"],
                }
                run.status = NightAuditRun.RunStatus.SUCCESS
                run.finished_at = now
                run.summary = summary
                run.save(update_fields=("status", "finished_at", "summary"))
                AuditEvent.record(
                    action="night_audit.run", entity_type="BusinessDate",
                    entity_id=bd.pk, actor=executed_by, property_obj=property_obj,
                    after={"business_date": day.isoformat(), **summary},
                    reason="PMS2 营业日夜审")
                return run, True
        except Exception as exc:
            NightAuditRun.objects.filter(pk=run.pk).update(
                status=NightAuditRun.RunStatus.FAILED,
                finished_at=timezone.now(), error_message=str(exc)[:4000],
            )
            raise

    @staticmethod
    @transaction.atomic
    def request_reopen(*, property_obj, business_date_id, requested_by, reason):
        reason = str(reason or "").strip()
        if not reason or len(reason) > 240:
            raise NightAuditError("重开营业日必须填写 1 至 240 个字符的原因。")
        bd = BusinessDate.objects.select_for_update().filter(
            pk=business_date_id, property=property_obj,
            status=BusinessDate.Status.CLOSED,
        ).first()
        if bd is None:
            raise NightAuditError("仅已关账营业日可以申请重开。")
        from apps.billing.services_v2 import ApprovalService
        approval, created = ApprovalService.request_approval(
            property_obj=property_obj, action_code="night_audit.reopen",
            entity_type="BusinessDate", entity_id=bd.pk,
            requested_by=requested_by, reason=reason,
            payload={"business_date": bd.business_date.isoformat()},
        )
        return approval, created

    @staticmethod
    @transaction.atomic
    def approve_reopen(*, property_obj, business_date_id, approval_id, approved_by):
        bd = BusinessDate.objects.select_for_update().filter(
            pk=business_date_id, property=property_obj,
            status=BusinessDate.Status.CLOSED,
        ).first()
        if bd is None:
            raise NightAuditError("营业日不存在或当前状态不允许重开。")
        approval = ApprovalRecord.objects.select_for_update().filter(
            pk=approval_id, property=property_obj,
            action_code="night_audit.reopen",
            entity_type="BusinessDate", entity_id=str(bd.pk),
        ).first()
        if approval is None:
            raise NightAuditError("找不到对应的营业日重开审批记录。")
        from apps.billing.services_v2 import ApprovalService
        # 只允许"待审批 → 本次批准"一条路径：已批准/已驳回/已过期的历史审批
        # 不得在营业日再次关账后被重放驱动第二次重开（重开必须有新申请事实）。
        if approval.status != ApprovalRecord.Status.PENDING:
            raise NightAuditError(
                f"重开申请已处理（{approval.status}），不能重复执行；"
                "如需再次重开请重新提交申请。")
        approval = ApprovalService.approve(
            approval.pk, approved_by=approved_by, property_obj=property_obj)
        if approval.status != ApprovalRecord.Status.APPROVED or approval.approved_by_id != approved_by.pk:
            raise NightAuditError("必须由当前审批人批准重开申请。")
        reason = approval.reason
        bd.status = BusinessDate.Status.REOPENED
        bd.reopened_at = timezone.now()
        bd.reopened_by = approved_by
        bd.reopen_reason = reason
        bd.save(update_fields=("status", "reopened_at", "reopened_by",
                               "reopen_reason", "open_property_key"))
        AuditEvent.record(
            action="night_audit.reopen", entity_type="BusinessDate",
            entity_id=bd.pk, actor=approved_by, property_obj=property_obj,
            after={"business_date": bd.business_date.isoformat(),
                   "status": bd.status, "approval_id": approval.pk},
            reason=reason)
        return bd


    @staticmethod
    @transaction.atomic
    def request_break_glass(*, property_obj, business_date_id, requested_by,
                            password, reason, validity_minutes=10):
        """仅单一有效经理账号的紧急场景发放十分钟、单营业日重开授权。"""
        from django.db.models import Q

        from apps.identity.models import StaffRoleAssignment

        reason = str(reason or "").strip()
        if len(reason) < 8 or len(reason) > 240:
            raise NightAuditError("紧急重开必须填写 8 至 240 个字符的具体原因。")
        if validity_minutes != 10:
            raise NightAuditError("Break-glass 授权窗口固定为 10 分钟。")
        if not password or not requested_by.check_password(password):
            raise NightAuditError("二次密码验证失败，未发放紧急授权。")

        now = timezone.now()
        eligible_users = set(
            StaffRoleAssignment.objects.filter(
                membership__property=property_obj,
                membership__status="ACTIVE",
                membership__staff_profile__employment_status="ACTIVE",
                membership__staff_profile__user__is_active=True,
                membership__staff_profile__user__isnull=False,
                starts_at__lte=now,
                group__permissions__content_type__app_label="hotelapp",
                group__permissions__codename="business_date_reopen",
            ).filter(
                Q(ends_at__isnull=True) | Q(ends_at__gt=now)
            ).values_list(
                "membership__staff_profile__user_id", flat=True
            ).distinct()
        )
        if eligible_users != {requested_by.pk}:
            raise NightAuditError(
                "当前物业存在其他有效营业日重开审批经理，必须走正常双人审批；"
                "Break-glass 仅适用于唯一有效经理账号的紧急情况。"
            )

        business_date = BusinessDate.objects.select_for_update().filter(
            pk=business_date_id, property=property_obj,
            status=BusinessDate.Status.CLOSED,
        ).first()
        if business_date is None:
            raise NightAuditError("仅已关账营业日可以申请紧急重开。")
        active = BreakGlassRecord.objects.select_for_update().filter(
            business_date=business_date,
            status=BreakGlassRecord.Status.GRANTED,
            expires_at__gt=now,
        ).first()
        if active:
            raise NightAuditError("该营业日已有有效的 Break-glass 授权。")

        record = BreakGlassRecord.objects.create(
            property=property_obj, business_date=business_date,
            requested_by=requested_by, reason=reason,
            password_verified_at=now,
            expires_at=now + timedelta(minutes=10),
            status=BreakGlassRecord.Status.GRANTED,
        )
        AuditEvent.record(
            action="night_audit.break_glass_requested",
            entity_type="BreakGlassRecord", entity_id=record.pk,
            actor=requested_by, property_obj=property_obj,
            after={"business_date": business_date.business_date.isoformat(),
                   "expires_at": record.expires_at.isoformat(),
                   "status": record.status},
            reason=reason)
        return record

    @staticmethod
    @transaction.atomic
    def execute_break_glass(*, property_obj, business_date_id, record_id,
                            executed_by):
        """执行单次限时授权；授权绑定申请人、物业和具体营业日。"""
        record = BreakGlassRecord.objects.select_for_update().filter(
            pk=record_id, property=property_obj,
            business_date_id=business_date_id,
        ).first()
        if record is None:
            raise NightAuditError("紧急授权记录不存在或不属于当前物业/营业日。")
        if record.requested_by_id != executed_by.pk:
            raise NightAuditError("紧急授权只能由原申请账号执行。")
        if record.status != BreakGlassRecord.Status.GRANTED:
            raise NightAuditError("紧急授权已使用、过期或失效，不能再次执行。")
        now = timezone.now()
        if record.expires_at <= now:
            record.status = BreakGlassRecord.Status.EXPIRED
            record.save(update_fields=("status",))
            AuditEvent.record(
                action="night_audit.break_glass_expired",
                entity_type="BreakGlassRecord", entity_id=record.pk,
                actor=executed_by, property_obj=property_obj,
                after={"status": record.status}, reason=record.reason)
            return None

        business_date = BusinessDate.objects.select_for_update().filter(
            pk=business_date_id, property=property_obj,
            status=BusinessDate.Status.CLOSED,
        ).first()
        if business_date is None:
            raise NightAuditError("营业日状态已变化，紧急授权未执行。")
        business_date.status = BusinessDate.Status.REOPENED
        business_date.reopened_at = now
        business_date.reopened_by = executed_by
        business_date.reopen_reason = record.reason
        business_date.save(update_fields=(
            "status", "reopened_at", "reopened_by", "reopen_reason",
            "open_property_key"))
        record.status = BreakGlassRecord.Status.USED
        record.used_at = now
        record.save(update_fields=("status", "used_at"))
        AuditEvent.record(
            action="night_audit.break_glass_executed",
            entity_type="BusinessDate", entity_id=business_date.pk,
            actor=executed_by, property_obj=property_obj,
            after={"business_date": business_date.business_date.isoformat(),
                   "status": business_date.status,
                   "break_glass_record_id": record.pk},
            reason=record.reason)
        return business_date

    @staticmethod
    @transaction.atomic
    def review_break_glass(*, property_obj, record_id, reviewed_by,
                           decision, reason):
        """事后独立复核；复核账号必须不同于紧急操作账号。"""
        from apps.identity.authorization import AuthorizationService

        reason = str(reason or "").strip()
        if len(reason) < 8 or len(reason) > 240:
            raise NightAuditError("事后复核必须填写 8 至 240 个字符的复核说明。")
        if decision not in (
            BreakGlassRecord.ReviewDecision.CONFIRMED,
            BreakGlassRecord.ReviewDecision.REJECTED,
        ):
            raise NightAuditError("复核结论只能是 CONFIRMED 或 REJECTED。")
        if not AuthorizationService.has_permission(
                reviewed_by, property_obj, "core.breakglass_review"):
            raise NightAuditError("当前账号没有紧急操作事后复核权限。")
        record = BreakGlassRecord.objects.select_for_update().filter(
            pk=record_id, property=property_obj,
        ).first()
        if record is None:
            raise NightAuditError("紧急授权记录不存在或不属于当前物业。")
        if record.requested_by_id == reviewed_by.pk:
            raise NightAuditError("紧急操作人与事后复核人不能是同一账号。")
        if record.status != BreakGlassRecord.Status.USED:
            raise NightAuditError("只有已执行的紧急操作可以进行事后复核。")
        record.status = BreakGlassRecord.Status.REVIEWED
        record.reviewed_by = reviewed_by
        record.reviewed_at = timezone.now()
        record.review_decision = decision
        record.review_reason = reason
        record.save(update_fields=(
            "status", "reviewed_by", "reviewed_at",
            "review_decision", "review_reason"))
        AuditEvent.record(
            action="night_audit.break_glass_reviewed",
            entity_type="BreakGlassRecord", entity_id=record.pk,
            actor=reviewed_by, property_obj=property_obj,
            after={"decision": decision, "status": record.status},
            reason=reason)
        return record
