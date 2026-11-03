# -*- coding: utf-8 -*-
"""PMS2 夜审领域服务：只对 PMS2 Stay/Folio/Ledger 写入，不读写旧 FinanceRecord。"""
from datetime import date
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from apps.billing.models import ApprovalRecord, Folio, LedgerEntry
from apps.core.models import AuditEvent, BusinessDate, NightAuditRun, Property
from apps.reservations.models import NoShowRecord, Reservation, ReservationRoom, Stay


class NightAuditError(Exception):
    """夜审业务校验失败。"""


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
        if business_date is None:
            return property_date(property_obj)
        if isinstance(business_date, str):
            try:
                return date.fromisoformat(business_date)
            except ValueError as exc:
                raise NightAuditError("营业日期必须是 YYYY-MM-DD。") from exc
        if not isinstance(business_date, date):
            raise NightAuditError("营业日期无效。")
        return business_date

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
        total = Decimal("0.00")
        for stay in stays:
            line = stay.reservation_room
            nightly = next(
                (item for item in (line.nightly_rates or [])
                 if str(item.get("date")) == day.isoformat()),
                None,
            )
            if nightly is None:
                raise NightAuditError(
                    f"在住单 {line.reservation.booking_no} 的房间明细 {line.pk} "
                    f"缺少 {day.isoformat()} 的每晚房价快照，夜审已阻断。"
                )
            try:
                amount = Decimal(str(nightly.get("price")))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise NightAuditError(
                    f"在住单 {line.reservation.booking_no} 的当日房价快照无效。"
                ) from exc
            if not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal("0.01")):
                raise NightAuditError(
                    f"在住单 {line.reservation.booking_no} 的当日房价金额无效。"
                )
            reference = f"night-audit:{property_obj.pk}:{day.isoformat()}:stay:{stay.pk}"
            already_posted = LedgerEntry.objects.filter(
                property=property_obj, reference=reference
            ).exists()
            rows.append({
                "stay_id": stay.pk,
                "booking_no": line.reservation.booking_no,
                "room_number": stay.room.number,
                "amount": str(amount),
                "currency": line.reservation.currency,
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
            "stay_count": len(rows),
            "charge_count": sum(1 for row in rows if not row["already_posted"]),
            "already_posted_count": sum(1 for row in rows if row["already_posted"]),
            "charge_total": str(total.quantize(Decimal("0.01"))),
            "currency": property_obj.currency,
            "no_show_review_candidates": candidates,
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
                Property.objects.select_for_update().get(pk=property_obj.pk)
                bd = BusinessDate.objects.select_for_update().get(pk=bd.pk)
                if bd.status not in (BusinessDate.Status.OPEN, BusinessDate.Status.REOPENED):
                    raise NightAuditError("营业日状态已变化，夜审未执行。")
                preview = cls.preview(property_obj=property_obj, business_date=day)
                posted = 0
                already = 0
                for row in preview["stays"]:
                    if row["already_posted"]:
                        already += 1
                        continue
                    if Decimal(row["amount"]) == 0:
                        # 零价房不创建零金额凭证，但保留在结果汇总。
                        already += 1
                        continue
                    folio = Folio.objects.select_for_update().filter(
                        property=property_obj,
                        reservation__booking_no=row["booking_no"],
                        folio_type=Folio.FolioType.MASTER,
                        status=Folio.Status.OPEN,
                    ).first()
                    if folio is None:
                        raise NightAuditError(
                            f"预订 {row['booking_no']} 没有有效主账单，夜审已整体回滚。"
                        )
                    stay = Stay.objects.get(pk=row["stay_id"], property=property_obj)
                    LedgerEntry.objects.create(
                        property=property_obj, folio=folio, stay=stay,
                        entry_type=LedgerEntry.EntryType.ROOM_CHARGE,
                        amount=Decimal(row["amount"]), currency=row["currency"],
                        business_date=day, reference=row["reference"],
                        description=f"夜审房费过租 {day.isoformat()}",
                        posted_by=executed_by,
                    )
                    posted += 1

                now = timezone.now()
                bd.status = BusinessDate.Status.CLOSED
                bd.closed_at = now
                bd.closed_by = executed_by
                bd.save(update_fields=("status", "closed_at", "closed_by", "open_property_key"))
                summary = {
                    "stay_count": preview["stay_count"],
                    "charge_count": posted,
                    "already_posted_count": already,
                    "charge_total": preview["charge_total"],
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
        if approval.status == ApprovalRecord.Status.PENDING:
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
