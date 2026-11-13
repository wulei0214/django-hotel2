"""支付意图领域服务：创建、状态迁移、渠道窗口策略（D2/D6）。

依据 docs/PMS2支付中心P0P1专项整改需求_2026-11-09.md PAY-P0-01~05：
- 意图与渠道交易分离；
- UNKNOWN 不得直接释放库存，须先查询渠道；
- 渠道窗口由服务端策略控制（客户端不得提交/修改窗口/延长上限）；
- 支付成功与 Hold 过期竞争时以条件迁移保证单一结果。
"""
import uuid
from contextlib import contextmanager
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.billing.models import (
    Payment,
    PaymentCompensationTask,
    PaymentIntent,
    ProviderTransaction,
    ProviderTransactionEvent,
)
from apps.core.booking_constants import PENDING_RESERVATION_TTL_MINUTES
from apps.core.models import AuditEvent

# D2：渠道支付窗口策略（服务端配置；客户端不得提交或修改）
# RSV-07：初始窗口必须与预订 expires_at 同源，否则会出现「库存已释放但
# 支付意图仍有效」或反之。
CHANNEL_WINDOW_CONFIG = {
    "mock": {
        "initial_wait_seconds": PENDING_RESERVATION_TTL_MINUTES * 60,
        "max_total_wait_seconds": 30 * 60,     # 最大累计等待 30 分钟
        "max_extensions": 1,                    # 最大延长次数
        "extension_seconds": 5 * 60,           # 每次延长 5 分钟
    },
}
DEFAULT_WINDOW = CHANNEL_WINDOW_CONFIG["mock"]

# WB-076：ProviderTransaction.channel_status 允许取值（渠道事实状态，
# 与意图状态机独立；模型未定义 choices）。WB-125（第 32 轮）：唯一权威
# 定义收口到 models.PROVIDER_CHANNEL_STATUSES——模型 save() 与服务层
# 校验共用同一白名单/迁移矩阵，防止两处口径漂移。
from apps.billing.models import PROVIDER_CHANNEL_STATUSES  # noqa: E402


def get_channel_window(provider_code):
    """D2：读取渠道支付窗口策略（服务端配置，不接受前端传入）。"""
    return CHANNEL_WINDOW_CONFIG.get(provider_code, DEFAULT_WINDOW)


@transaction.atomic
def create_intent(*, property_obj, reservation, folio, amount, currency="CNY",
                  purpose="ROOM_FEE", provider_code="mock", created_by=None,
                  idempotency_key=""):
    """创建支付意图（幂等）。创建时关联有效 Hold 并设置初始窗口。

    WB-108 收尾：意图创建前对预订侧做完整前置校验（校验先于幂等重放
    查找，不得以重放为名绕过前置）——
    ① 与取消/过期/Hold 释放仲裁共用 Reservation 行锁；
    ② 状态机口径与公开模拟支付服务一致：PENDING 可付；CONFIRMED 允许
       补建意图（已成交，无需 Hold）；CANCELLED/EXPIRED/CHECKED_IN 及
       之后的状态拒绝；
    ③ PENDING 必须有未过期的支付截止时间（expires_at 语义仅适用于
       待支付状态）；
    ④ PENDING 必须存在有效库存 Hold（口径见下方注释）；
    ⑤ 意图金额必须等于订单应付总额（公开支付链路即全款支付）。
    """
    from apps.reservations.models import Reservation

    key = str(idempotency_key or "").strip()
    if not key or len(key) > 96:
        raise ValueError("支付意图必须提供 1 至 96 个字符的幂等键。")
    intent_amount = Decimal(str(amount)).quantize(Decimal("0.01"))
    if intent_amount <= 0:
        raise ValueError("支付意图金额必须大于零。")

    # WB-008 修复：跨表业务归属一致性校验
    if reservation.property_id != property_obj.pk:
        raise ValueError("预订不属于当前物业。")
    if folio.property_id != property_obj.pk:
        raise ValueError("账单不属于当前物业。")
    if folio.reservation_id != reservation.pk:
        raise ValueError("账单与预订不一致。")
    if folio.currency != reservation.currency:
        raise ValueError("账单币种与预订币种不一致。")
    # WB-071 修复：传入币种必须与账单、预订币种一致（原先只校验账单 vs 预订）
    if currency != folio.currency:
        raise ValueError("支付意图币种与账单币种不一致。")
    if currency != reservation.currency:
        raise ValueError("支付意图币种与预订币种不一致。")

    # WB-108 ①：与取消/过期/Hold 释放竞争同一 Reservation 行锁后，
    # 后续状态/窗口/Hold/金额读到的都是锁内真值（MySQL 行锁仲裁；
    # 本事务可能已持有该锁——create_mock_payment_intent 先锁预订）。
    try:
        reservation = Reservation.objects.select_for_update().get(
            pk=reservation.pk)
    except Reservation.DoesNotExist as exc:
        raise ValueError("预订不存在，不能创建支付意图。") from exc

    # WB-108 ②：状态机口径与 create_mock_payment_intent 保持一致——
    # PENDING 是唯一"待支付"状态；CONFIRMED 已成交，允许补建意图但
    # 无需 Hold；取消/过期/入住及之后的终态不得再产生新的待支付意图。
    if reservation.status not in (Reservation.Status.PENDING,
                                  Reservation.Status.CONFIRMED):
        raise ValueError(
            "当前预订状态不允许创建支付意图（仅待支付/已确认订单可支付）。")

    if reservation.status == Reservation.Status.PENDING:
        # WB-108 ③：待支付订单必须存在未过期的支付截止时间
        # （与 create_mock_payment_intent 的超时拒绝同口径）。
        if reservation.expires_at is None:
            raise ValueError("待支付订单缺少支付截止时间，不能创建支付意图。")
        if reservation.expires_at <= timezone.now():
            raise ValueError("预订已超过支付截止时间，请重新查询房态后下单。")

        # WB-108 ④：有效 Hold 判定与 confirm_inventory_for_reservation
        # 同口径——冻结的权威载体是 RoomTypeInventory.held 计数；
        # InventoryHold 明细行（RSV-01 起由库存服务同事务维护）用于逐单
        # 追溯与对账重建，不作为支付存在性判据。预订每个（房型, 入住-离店）
        # 区间的每个住宿日都必须有库存行且 held ≥ 需求量，否则视为
        # Hold 已释放/失效，拒绝为不可履约的订单创建支付意图。
        from apps.inventory.models import RoomTypeInventory
        from apps.inventory.services import (
            _group_reservation_room_quantities,
            stay_dates,
        )
        for (room_type_id, check_in, check_out), quantity in (
                _group_reservation_room_quantities(reservation).items()):
            dates = list(stay_dates(check_in, check_out))
            rows = RoomTypeInventory.objects.filter(
                property_id=reservation.property_id,
                room_type_id=room_type_id,
                stay_date__in=dates,
            )
            if (rows.count() != len(dates)
                    or any(row.held < quantity for row in rows)):
                raise ValueError(
                    "预订库存 Hold 已失效或不存在，不能创建支付意图。")

    # WB-108 ⑤：金额服务端核对——公开支付链路即全款支付，现有全部调用方
    # （create_mock_payment_intent 以 reservation.total_amount 建单）均为
    # 全额口径；意图金额必须等于订单应付总额，杜绝部分金额意图绕过
    # "付清即确认"的语义。
    if intent_amount != reservation.total_amount.quantize(Decimal("0.01")):
        raise ValueError("支付意图金额必须与订单应付总额一致。")

    existing = PaymentIntent.objects.filter(idempotency_key=key).first()
    if existing is not None:
        # WB-069 修复：幂等重放比较补齐 currency/purpose/provider_code
        if (existing.property_id != property_obj.pk
                or existing.reservation_id != reservation.pk
                or existing.amount != intent_amount
                or existing.folio_id != folio.pk
                or existing.currency != currency
                or existing.purpose != purpose
                or existing.provider_code != provider_code):
            raise ValueError("该幂等键已用于不同支付意图。")
        return existing, False

    window = get_channel_window(provider_code)
    now = timezone.now()
    intent = PaymentIntent.objects.create(
        property=property_obj, reservation=reservation, folio=folio,
        intent_no=f"PI-{uuid.uuid4().hex.upper()[:20]}",
        idempotency_key=key,
        amount=intent_amount, currency=currency,
        purpose=purpose,
        provider_code=provider_code,
        provider_config_version="v1",
        status=PaymentIntent.Status.PENDING,
        expires_at=now + timezone.timedelta(seconds=window["initial_wait_seconds"]),
        max_expires_at=now + timezone.timedelta(seconds=window["max_total_wait_seconds"]),
        created_by=created_by)
    AuditEvent.record(
        action="billing.payment_intent_created", entity_type="PaymentIntent",
        entity_id=intent.pk, actor=created_by, property_obj=property_obj,
        after={"intent_no": intent.intent_no, "amount": str(intent_amount),
               "provider_code": provider_code, "expires_at": intent.expires_at.isoformat()},
        reason="创建支付意图")
    return intent, True


@transaction.atomic
def extend_intent_window(*, intent_id, property_obj, extended_by=None):
    """D2：有限延长支付窗口（仅 PENDING/PROCESSING；有上限）。"""
    intent = PaymentIntent.objects.select_for_update().filter(
        pk=intent_id, property=property_obj).first()
    if intent is None:
        raise ValueError("支付意图不存在或不属于当前物业。")
    if intent.status not in (PaymentIntent.Status.PENDING,
                             PaymentIntent.Status.PROCESSING):
        raise ValueError("只有待支付/处理中的意图可以延长窗口。")
    window = get_channel_window(intent.provider_code)
    if intent.extension_count >= window["max_extensions"]:
        raise ValueError("已达到最大延长次数，转入补偿核查。")
    if intent.max_expires_at and intent.expires_at and (
            intent.expires_at + timezone.timedelta(seconds=window["extension_seconds"])
            > intent.max_expires_at):
        raise ValueError("已达到最大累计等待时长，转入补偿核查。")
    intent.extension_count += 1
    intent.expires_at = (intent.expires_at or timezone.now()) + timezone.timedelta(
        seconds=window["extension_seconds"])
    intent.save(update_fields=("extension_count", "expires_at", "updated_at"))
    AuditEvent.record(
        action="billing.intent_window_extended", entity_type="PaymentIntent",
        entity_id=intent.pk, actor=extended_by, property_obj=property_obj,
        after={"extension_count": intent.extension_count,
               "expires_at": intent.expires_at.isoformat()},
        reason="D2 窗口延长")
    return intent


class _ProviderEventReplayConflict(Exception):
    """WB-115：同一 event_id 重放携带不同事实（携带冲突双方快照）。

    在原子块内抛出会让整块回滚——若把安全审计写在块内，审计证据会
    随回滚消失。公开入口在独立事务中消费本异常落审计后再转 ValueError。
    """

    def __init__(self, existing, *, event_id, provider_code,
                 provider_transaction_id, incoming_amount, currency,
                 channel_status, signature_verified):
        self.entity_id = existing.pk
        self.detail = {
            "event_id": event_id,
            "provider_code": provider_code,
            "stored": {
                "provider_transaction_id": existing.provider_transaction_id,
                "amount": str(existing.amount),
                "currency": existing.currency,
                "channel_status": existing.channel_status,
                "signature_verified": bool(existing.signature_verified),
            },
            "incoming": {
                "provider_transaction_id": provider_transaction_id,
                "amount": str(incoming_amount),
                "currency": currency,
                "channel_status": channel_status,
                "signature_verified": bool(signature_verified),
            },
        }
        super().__init__(self.detail)


def _validate_provider_event_facts(intent, provider_code, incoming_amount,
                                   currency, channel_status,
                                   signature_verified):
    """WB-076/089：首次写入渠道事实前做完整一致性校验。"""
    if provider_code != intent.provider_code:
        raise ValueError("渠道代码与支付意图的渠道不一致，不能记录渠道交易事实。")
    if incoming_amount != intent.amount:
        raise ValueError("渠道交易金额与支付意图金额不一致。")
    if currency != intent.currency:
        raise ValueError("渠道交易币种与支付意图币种不一致。")
    if channel_status not in PROVIDER_CHANNEL_STATUSES:
        raise ValueError(
            f"渠道状态 {channel_status} 不在允许取值内"
            f"（{sorted(PROVIDER_CHANNEL_STATUSES)}）。")
    if channel_status == "SUCCESS" and not signature_verified:
        raise ValueError(
            "渠道状态为 SUCCESS 时必须提供验签通过结果"
            "（signature_verified=True），拒绝未验签的成功事实。")


@contextmanager
def _provider_transition_authorization():
    """WB-125（第 32 轮）：受控状态机推进的唯一授权入口，收口在支付领域
    服务（私有函数）。models 不再公开上下文管理器；即使其他模块直接
    操作线程本地令牌，模型 save() 仍按白名单 + 迁移矩阵二次校验。
    """
    from apps.billing.models import _provider_transition_state
    _provider_transition_state.authorized = True
    try:
        yield
    finally:
        _provider_transition_state.authorized = False


def _transition_provider_transaction(pt, *, channel_status,
                                     signature_verified):
    """WB-125：唯一允许改写渠道交易主记录的受控路径（状态机推进）。

    第 31 轮：不再使用可被任意调用方设置的实例属性放行；改为进入
    _provider_transition_authorization() 线程本地受控上下文，模型 save()
    同时按事实比对 + 状态机规则校验迁移合法性（双保险）。第 32 轮：
    受控入口收口为支付领域服务私有函数；模型层在上下文内仍二次校验
    白名单与迁移矩阵。事件历史另存于 ProviderTransactionEvent，永不修改。
    """
    with _provider_transition_authorization():
        pt.channel_status = channel_status
        pt.signature_verified = signature_verified
        if channel_status == "SUCCESS":
            pt.provider_confirmed_at = (
                pt.provider_confirmed_at or timezone.now())
        pt.save(update_fields=("channel_status", "signature_verified",
                               "provider_confirmed_at"))


@transaction.atomic
def _record_provider_event_atomic(*, property_obj, intent_id, provider_code,
                                  provider_transaction_id, amount, currency,
                                  channel_status, event_id="",
                                  signature_verified=False):
    from apps.core.models import AuditEvent as AE
    intent = PaymentIntent.objects.filter(pk=intent_id, property=property_obj).first()
    if intent is None:
        raise ValueError("支付意图不存在或不属于当前物业。")
    incoming_amount = Decimal(str(amount)).quantize(Decimal("0.01"))
    event_id = (event_id or "").strip()

    # ---- 事件幂等锚点：provider_code + event_id（事件历史表，WB-119）----
    existing_event = None
    if event_id:
        existing_event = ProviderTransactionEvent.objects.filter(
            provider_code=provider_code,
            provider_event_key=f"{provider_code}:{event_id}").first()
    if existing_event is not None:
        # WB-115：幂等重放必须核对**完整不可变事实**——channel_status 与
        # signature_verified 也是渠道事件事实的一部分。同一 event_id 重放
        # 携带任何不同事实（SUCCESS 改报 FAILED、已验签改报未验签、
        # PENDING 重放改报 SUCCESS 等）一律视为冲突重放：拒绝并落安全
        # 审计事件（由公开入口在独立事务落盘，不随本块回滚），不得静默
        # 返回第一次记录，也不得用第二次请求的验签结果覆盖/补齐第一次
        # 记录。渠道状态变化必须由渠道下发**新的事件 ID**表达。
        if (existing_event.property_id != property_obj.pk
                or existing_event.intent_id != intent.pk
                or existing_event.provider_transaction_id != provider_transaction_id
                or existing_event.amount != incoming_amount
                or existing_event.currency != currency
                or existing_event.channel_status != channel_status
                or bool(existing_event.signature_verified) != bool(signature_verified)):
            raise _ProviderEventReplayConflict(
                existing_event, event_id=event_id, provider_code=provider_code,
                provider_transaction_id=provider_transaction_id,
                incoming_amount=incoming_amount, currency=currency,
                channel_status=channel_status,
                signature_verified=signature_verified)
        main = ProviderTransaction.objects.filter(
            provider_code=provider_code,
            provider_transaction_id=provider_transaction_id).first()
        return main, False

    _validate_provider_event_facts(intent, provider_code, incoming_amount,
                                   currency, channel_status,
                                   signature_verified)

    # ---- WB-119：主记录按（渠道, 交易号）行锁定位，存在则受控推进 ----
    main = ProviderTransaction.objects.select_for_update().filter(
        provider_code=provider_code,
        provider_transaction_id=provider_transaction_id).first()
    if main is None:
        try:
            # 保存点：并发首建同交易号（间隙锁不阻止 INSERT）撞
            # uq_pt_provider_txn 时只回滚本块，回查后改走追加路径。
            with transaction.atomic():
                main = ProviderTransaction.objects.create(
                    property=property_obj, intent=intent,
                    provider_code=provider_code,
                    provider_transaction_id=provider_transaction_id,
                    event_id=event_id, amount=incoming_amount,
                    currency=currency, channel_status=channel_status,
                    signature_verified=signature_verified)
                ProviderTransactionEvent.objects.create(
                    property=property_obj, intent=intent, transaction=main,
                    provider_code=provider_code,
                    provider_transaction_id=provider_transaction_id,
                    event_id=event_id, amount=incoming_amount,
                    currency=currency, channel_status=channel_status,
                    signature_verified=signature_verified)
                AE.record(
                    action="billing.provider_event_recorded",
                    entity_type="ProviderTransaction",
                    entity_id=main.pk, property_obj=property_obj,
                    after={"provider_transaction_id": provider_transaction_id,
                           "channel_status": channel_status,
                           "event_id": event_id},
                    reason="记录渠道交易事实")
        except IntegrityError:
            main = ProviderTransaction.objects.select_for_update().filter(
                provider_code=provider_code,
                provider_transaction_id=provider_transaction_id).first()
            if main is None:
                raise
        else:
            return main, True

    # 同交易号新事件：归属必须一致（数据库唯一键保证同一交易号只归属
    # 一个意图；此处防止跨物业/跨意图/金额币种漂移的事件追加）。
    if (main.property_id != property_obj.pk
            or main.intent_id != intent.pk
            or main.amount != incoming_amount
            or main.currency != currency):
        raise _ProviderEventReplayConflict(
            main, event_id=event_id, provider_code=provider_code,
            provider_transaction_id=provider_transaction_id,
            incoming_amount=incoming_amount, currency=currency,
            channel_status=channel_status,
            signature_verified=signature_verified)
    # 状态机：SUCCESS 是终态，不得倒退（迟到成功不可逆；失败/未知不覆盖成功）。
    if main.channel_status == "SUCCESS" and channel_status != "SUCCESS":
        raise ValueError(
            f"渠道交易 {provider_transaction_id} 已成功（终态），不得倒退为 "
            f"{channel_status}；拒绝该渠道事件，请核对渠道通知内容。")
    ProviderTransactionEvent.objects.create(
        property=property_obj, intent=intent, transaction=main,
        provider_code=provider_code,
        provider_transaction_id=provider_transaction_id,
        event_id=event_id, amount=incoming_amount, currency=currency,
        channel_status=channel_status, signature_verified=signature_verified)
    if (main.channel_status != channel_status
            or bool(main.signature_verified) != bool(signature_verified)):
        _transition_provider_transaction(
            main, channel_status=channel_status,
            signature_verified=signature_verified)
    AE.record(
        action="billing.provider_event_appended",
        entity_type="ProviderTransaction",
        entity_id=main.pk, property_obj=property_obj,
        after={"provider_transaction_id": provider_transaction_id,
               "channel_status": channel_status, "event_id": event_id,
               "previous_channel_status": main.channel_status},
        reason="追加渠道事件并按状态机推进主记录（WB-119）")
    return main, True


def record_provider_event(*, property_obj, intent_id, provider_code,
                          provider_transaction_id, amount, currency,
                          channel_status, event_id="", signature_verified=False):
    """记录渠道交易事实（事件历史 + 主记录受控状态机，WB-119/125）。

    - 事件幂等：同 event_id 完整事实一致 → 幂等返回；任何事实不一致
      （含渠道状态、验签结果）→ 拒绝（WB-115）；
    - 同交易号新事件 ID → 追加不可变事件历史，并按受控状态机推进主记录
      （SUCCESS 终态不可倒退），不再撞交易号唯一约束（WB-119）；
    - 主记录实例级改写被模型禁止，仅状态机受控放行（WB-125）；
    - 冲突重放的安全审计在**独立事务**中落盘——主原子块回滚不能吞掉
      审计证据（与 expire_intent 落 CHANNEL_QUERY 任务同模式）。
    """
    try:
        return _record_provider_event_atomic(
            property_obj=property_obj, intent_id=intent_id,
            provider_code=provider_code,
            provider_transaction_id=provider_transaction_id,
            amount=amount, currency=currency,
            channel_status=channel_status, event_id=event_id,
            signature_verified=signature_verified)
    except _ProviderEventReplayConflict as conflict:
        from apps.core.models import AuditEvent as AE
        with transaction.atomic():
            AE.record(
                action="billing.provider_event_replay_conflict",
                entity_type="ProviderTransaction",
                entity_id=conflict.entity_id,
                property_obj=property_obj,
                after=conflict.detail,
                reason="渠道事件事实冲突，拒绝重放（WB-115/119）")
        raise ValueError(
            "事件 ID 已用于不同的物业/意图/交易号/金额/币种/渠道状态/"
            "验签结果，不能复用已有渠道交易记录；渠道状态变化请使用"
            "新的渠道事件 ID 表达。") from conflict


@transaction.atomic
def confirm_intent_success(*, property_obj, intent_id, provider_transaction_id,
                           confirmed_payment=None, actor=None):
    """渠道确认成功 → 意图 SUCCEEDED + 关联 Payment + Hold 确认（同事务）。

    竞态仲裁：本方法与 Hold 过期释放共用数据库行锁，保证渠道成功后
    Hold 不被过期任务释放（PAY-P0-04）。
    """
    from apps.inventory.services import InventoryUnavailable, confirm_inventory_for_reservation
    intent = PaymentIntent.objects.select_for_update().filter(
        pk=intent_id, property=property_obj).first()
    if intent is None:
        raise ValueError("支付意图不存在或不属于当前物业。")
    if intent.status == PaymentIntent.Status.SUCCEEDED:
        return intent, False
    if intent.status in (PaymentIntent.Status.FAILED,
                         PaymentIntent.Status.EXPIRED,
                         PaymentIntent.Status.CANCELLED):
        raise ValueError("已终态意图不可确认成功。")
    if confirmed_payment is None:
        raise ValueError(
            "WB-011：确认成功必须关联真实 Payment；"
            "不得产生意图显示成功但没有对应收款事实的状态。")
    # WB-072 修复：确认前对收款事实做完整归属/金额/币种/状态/占用校验
    if confirmed_payment.status != Payment.Status.SUCCEEDED:
        raise ValueError("确认成功必须关联 SUCCEEDED 状态的收款事实。")
    if (confirmed_payment.property_id != intent.property_id
            or confirmed_payment.reservation_id != intent.reservation_id
            or confirmed_payment.folio_id != intent.folio_id):
        raise ValueError("收款事实与支付意图的物业/预订/账单归属不一致。")
    if (confirmed_payment.amount != intent.amount
            or confirmed_payment.currency != intent.currency):
        raise ValueError("收款事实金额或币种与支付意图不一致。")
    if PaymentIntent.objects.filter(confirmed_payment=confirmed_payment).exclude(
            pk=intent.pk).exists():
        raise ValueError("该收款事实已被其他支付意图关联，不可重复确认。")
    # WB-107 修复：确认前核验渠道交易事实——支付成功确认必须有本意图名下、
    # 已验签的 SUCCESS 渠道交易支撑，杜绝仅凭本地 Payment 伪造渠道成功：
    # ① 按（物业=意图物业, 意图, 渠道交易号）锁定查询；查不到即交易不存在，
    #    或交易属于其他意图/物业（唯一约束保证同一交易号只归属一个意图）；
    # ② 渠道代码/状态/验签/金额/币种必须与意图一致。
    provider_txn = ProviderTransaction.objects.select_for_update().filter(
        property_id=intent.property_id,
        intent_id=intent.pk,
        provider_transaction_id=provider_transaction_id,
    ).first()
    if provider_txn is None:
        raise ValueError(
            "渠道交易不存在或不属于该意图，不能确认支付成功。")
    if provider_txn.provider_code != intent.provider_code:
        raise ValueError("渠道交易渠道代码与支付意图不一致，不能确认支付成功。")
    if provider_txn.channel_status != "SUCCESS":
        raise ValueError(
            f"渠道交易状态为 {provider_txn.channel_status} 而非 SUCCESS，"
            "不能确认支付成功。")
    if not provider_txn.signature_verified:
        raise ValueError("渠道交易未通过验签，不能确认支付成功。")
    if (provider_txn.amount != intent.amount
            or provider_txn.currency != intent.currency):
        raise ValueError("渠道交易金额或币种与支付意图不一致，不能确认支付成功。")
    # WB-107 收尾：收款事实与渠道交易事实必须双向绑定。此前只校验了归属/
    # 金额/币种，属性一致但属于另一笔渠道交易的 Payment 仍可能被配对本意图。
    if confirmed_payment.provider != provider_txn.provider_code:
        raise ValueError(
            "收款事实的支付渠道与渠道交易渠道不一致，不能确认支付成功。")
    if (confirmed_payment.provider_ref
            != provider_txn.provider_transaction_id):
        raise ValueError(
            "收款事实的渠道流水号与渠道交易号不一致，不能确认支付成功。")
    intent.confirmed_payment = confirmed_payment
    intent.status = PaymentIntent.Status.SUCCEEDED
    intent.save(update_fields=("status", "confirmed_payment", "updated_at"))
    try:
        confirm_inventory_for_reservation(intent.reservation)
    except InventoryUnavailable:
        # WB-073 修复：Hold 已失效不再静默吞掉——创建持久化补偿任务，
        # 资金事实保留，转入高优先级异常处理（D3 P0）
        task, _ = create_compensation_task(
            task_type=PaymentCompensationTask.TaskType.HOLD_COORDINATION,
            aggregate_type="PaymentIntent", aggregate_id=intent.pk,
            idempotency_key=f"intent-inventory-confirm:{intent.pk}")
        # 模型无独立 payload 字段：预订/意图标识与触发原因写入错误摘要，
        # 并落审计事件（create_compensation_task 自身亦审计）
        task.last_error_code = "INVENTORY_CONFIRM_UNAVAILABLE"
        task.last_error_summary = (
            f"库存确认失败：reservation={intent.reservation_id} "
            f"intent={intent.intent_no} 的 Hold 已失效/释放，需高优先级协调")
        task.save(update_fields=("last_error_code", "last_error_summary"))
        AuditEvent.record(
            action="billing.intent_inventory_compensation_created",
            entity_type="PaymentCompensationTask", entity_id=task.pk,
            actor=actor, property_obj=property_obj,
            after={"task_id": task.pk, "intent_no": intent.intent_no,
                   "reservation_id": intent.reservation_id},
            reason="渠道确认成功但库存 Hold 失效，创建 HOLD_COORDINATION 补偿任务")
    AuditEvent.record(
        action="billing.intent_confirmed", entity_type="PaymentIntent",
        entity_id=intent.pk, actor=actor, property_obj=property_obj,
        after={"status": "SUCCEEDED",
               "provider_transaction_id": provider_transaction_id},
        reason="渠道确认成功（PAY-P0-04 竞态仲裁）")
    return intent, True


class ChannelStateUnknownError(ValueError):
    """WB-121：渠道事实 UNKNOWN 的专用信号。

    不再用通用 ValueError 表达多个不同业务结果——调度/统计方据此把
    「渠道未知」与「状态竞态/业务拒绝」分开计数，UNKNOWN 指标不失真。
    仍继承 ValueError：既有按 ValueError 处理的调用方语义不变。
    """


def expire_intent(*, property_obj, intent_id, actor=None):
    """意图过期（窗口到期且渠道未确认成功方可过期；UNKNOWN 不走此路径）。

    WB-090 收尾：状态迁移本身在单个原子块内；渠道 UNKNOWN 时原子块回滚
    （意图保持原状态），随后在**独立事务**中落 CHANNEL_QUERY 补偿任务，
    再向调用方抛 ValueError。若把任务创建放在同一原子块里，抛异常时任务
    会随事务一起回滚，「先查渠道再决策」在生产链路上将永远不可达。
    """
    try:
        with transaction.atomic():
            return _expire_intent_atomic(
                property_obj=property_obj, intent_id=intent_id, actor=actor)
    except ChannelStateUnknownError:
        create_compensation_task(
            task_type=PaymentCompensationTask.TaskType.CHANNEL_QUERY,
            aggregate_type="PaymentIntent", aggregate_id=intent_id,
            idempotency_key=f"intent-expire-channel-query:{intent_id}")
        # WB-121（第 31 轮）：继续抛出 ChannelStateUnknownError，不再
        # 重新包装成普通 ValueError——否则调度层 except
        # ChannelStateUnknownError 分支永远不可达，真实 UNKNOWN 会被
        # 误计入状态冲突统计。该异常继承 ValueError，既有调用方语义不变。
        raise ChannelStateUnknownError(
            "渠道状态未知（UNKNOWN），必须先查询渠道最终结果；"
            "不可直接过期，已创建渠道查询补偿任务。")


def _expire_intent_atomic(*, property_obj, intent_id, actor=None):
    intent = PaymentIntent.objects.select_for_update().filter(
        pk=intent_id, property=property_obj).first()
    if intent is None:
        raise ValueError("支付意图不存在或不属于当前物业。")
    if intent.status not in (PaymentIntent.Status.PENDING,
                             PaymentIntent.Status.CREATED):
        raise ValueError("只有待支付状态的意图可以过期。")
    # WB-077/090 修复：必须真实到期才允许过期，防止提前作废未到期意图
    if intent.expires_at is None or intent.expires_at > timezone.now():
        raise ValueError("支付窗口尚未到期，不可将意图标记为过期。")
    # 渠道侧状态未知时，PMS 未掌握渠道最终结果，直接释放 Hold / 标记过期
    # 可能造成「渠道已收款但订单被作废」。
    if ProviderTransaction.objects.filter(
            intent_id=intent.pk, channel_status="UNKNOWN").exists():
        raise ChannelStateUnknownError()
    intent.status = PaymentIntent.Status.EXPIRED
    intent.save(update_fields=("status", "updated_at"))
    AuditEvent.record(
        action="billing.intent_expired", entity_type="PaymentIntent",
        entity_id=intent.pk, actor=actor, property_obj=property_obj,
        after={"status": "EXPIRED"}, reason="窗口到期未支付")
    return intent


def _find_compensation_task(task_type, aggregate_type, aggregate_id,
                            idempotency_key):
    """按幂等键回查任务；事实（类型/聚合）不一致即拒绝复用。"""
    task = PaymentCompensationTask.objects.filter(
        idempotency_key=idempotency_key).first()
    if task is not None and (task.task_type != task_type
                             or task.aggregate_type != aggregate_type
                             or task.aggregate_id != aggregate_id):
        raise ValueError(
            f"幂等键 {idempotency_key} 已被用于不同的补偿任务事实"
            "（类型/聚合不一致），不能复用。")
    return task


@transaction.atomic
def create_compensation_task(*, task_type, aggregate_type, aggregate_id,
                             idempotency_key, max_attempts=5):
    """创建持久化补偿任务（幂等）。

    WB-126：并发创建方同时通过预查询后，第二个 create 会撞 idempotency_key
    唯一键。用嵌套保存点把冲突回滚收敛到本块，再按幂等键回查、核对事实后
    幂等返回已有任务——两个调度进程都不再 500/中断本轮维护。
    """
    existing = _find_compensation_task(task_type, aggregate_type,
                                       aggregate_id, idempotency_key)
    if existing is not None:
        return existing, False
    try:
        with transaction.atomic():  # 嵌套保存点：唯一键竞争只回滚本块
            task = PaymentCompensationTask.objects.create(
                task_type=task_type, aggregate_type=aggregate_type,
                aggregate_id=aggregate_id, idempotency_key=idempotency_key,
                max_attempts=max_attempts,
                next_attempt_at=timezone.now())
    except IntegrityError:
        existing = _find_compensation_task(task_type, aggregate_type,
                                           aggregate_id, idempotency_key)
        if existing is None:
            raise
        return existing, False
    AuditEvent.record(
        action="billing.compensation_task_created",
        entity_type="PaymentCompensationTask", entity_id=task.pk,
        after={"task_type": task_type, "aggregate_id": aggregate_id},
        reason="创建支付补偿任务（D3 P0）")
    return task, True


@transaction.atomic
def lease_compensation_task(task_id, lease_owner, lease_seconds=60):
    """数据库租约抢占：两个 worker 同时抢占，最多一个有效执行者。"""
    task = PaymentCompensationTask.objects.select_for_update().filter(
        pk=task_id).first()
    if task is None:
        raise ValueError("补偿任务不存在。")
    now = timezone.now()
    if task.status == PaymentCompensationTask.Status.LEASED and (
            task.lease_expires_at and task.lease_expires_at > now):
        raise ValueError("任务已被其他 worker 抢占且租约未到期。")
    if task.status not in (PaymentCompensationTask.Status.PENDING,
                           PaymentCompensationTask.Status.LEASED):
        raise ValueError("只有待执行/已抢占的任务可以租约执行。")
    task.status = PaymentCompensationTask.Status.LEASED
    task.lease_owner = lease_owner
    task.lease_expires_at = now + timezone.timedelta(seconds=lease_seconds)
    task.attempt_count += 1
    task.started_at = now
    task.save(update_fields=("status", "lease_owner", "lease_expires_at",
                             "attempt_count", "started_at"))
    return task


@transaction.atomic
def complete_compensation_task(task_id, success=True, error_code="",
                               error_summary="", lease_owner=""):
    """完成任务：成功或超限进死信。WB-006/WB-067 修复：强制校验租约持有者。"""
    task = PaymentCompensationTask.objects.select_for_update().filter(
        pk=task_id).first()
    if task is None:
        raise ValueError("补偿任务不存在。")
    # WB-067 修复：强制租约校验，空值不得绕过比对——
    # 任务必须已被领取、调用方必须声明身份、两者必须一致
    if not task.lease_owner:
        raise ValueError("任务尚未被领取（无租约持有者），不可提交执行结果。")
    if not lease_owner:
        raise ValueError("完成任务必须声明租约持有者（lease_owner 必填）。")
    if task.lease_owner != lease_owner:
        raise ValueError(
            f"租约持有者不匹配：当前 {task.lease_owner}，调用方 {lease_owner}。"
            "旧 Worker 不得覆盖新 Worker 的执行状态。")
    # WB-067：完成任务必须发生在活跃租约（status=LEASED）内——任务已不在
    # LEASED 状态（SUCCEEDED/DEAD_LETTER 终态，或失败退避已回 PENDING）
    # 说明本次租约已收尾，重复提交属于越权写入：终态不得被复活、退避重试
    # 不得被重复计数（同一租约下重复 complete 只生效一次）。
    if task.status != PaymentCompensationTask.Status.LEASED:
        raise ValueError(
            "任务不在租约执行状态（已终态或已退回待执行），"
            "重复提交执行结果被拒绝。")
    # WB-067：租约未过期才可提交；过期视为租约失效，拒绝提交。
    if (task.lease_expires_at is None
            or task.lease_expires_at <= timezone.now()):
        raise ValueError("租约已过期，视为租约失效，拒绝提交执行结果。")
    if success:
        task.status = PaymentCompensationTask.Status.SUCCEEDED
        task.completed_at = timezone.now()
        # WB-110：成功执行也可附带执行报告（如 mock 渠道查询只读汇报、
        # 对账扫描摘要），写入错误摘要字段供运维查询（空值则保持原样）。
        task.last_error_code = (error_code or "")[:64]
        task.last_error_summary = (error_summary or "")[:500]
        task.save(update_fields=("status", "completed_at", "last_error_code",
                                 "last_error_summary"))
        return task
    # WB-110：不可自动恢复的失败（如预订已进入取消/过期等终态、账本凭证
    # 无法安全重建）重试没有意义——跳过指数退避直接进入死信，等待人工
    # 协调/对账；原因写入 last_error_summary（payload），支付等资金事实
    # 一律保留、不得篡改。人工排查后仍可 replay_dead_letter_task 重放。
    if error_code == "REQUIRES_MANUAL_COORDINATION" or task.attempt_count >= task.max_attempts:
        task.status = PaymentCompensationTask.Status.DEAD_LETTER
        task.completed_at = timezone.now()
    else:
        # 指数退避
        backoff = min(2 ** task.attempt_count * 30, 300)
        task.status = PaymentCompensationTask.Status.PENDING
        task.next_attempt_at = timezone.now() + timezone.timedelta(seconds=backoff)
    task.last_error_code = error_code[:64]
    task.last_error_summary = error_summary[:500]
    task.save(update_fields=("status", "completed_at", "last_error_code",
                             "last_error_summary", "next_attempt_at"))
    return task


@transaction.atomic
def replay_dead_letter_task(task_id, operator, reason=""):
    """人工重放死信任务：复用原意图幂等键，不新建第二笔支付事实。"""
    task = PaymentCompensationTask.objects.select_for_update().filter(
        pk=task_id).first()
    if task is None:
        raise ValueError("补偿任务不存在。")
    if task.status != PaymentCompensationTask.Status.DEAD_LETTER:
        raise ValueError("只有死信任务可以人工重放。")
    task.status = PaymentCompensationTask.Status.PENDING
    task.attempt_count = 0
    task.next_attempt_at = timezone.now()
    task.save(update_fields=("status", "attempt_count", "next_attempt_at"))
    AuditEvent.record(
        action="billing.compensation_task_replayed",
        entity_type="PaymentCompensationTask", entity_id=task.pk,
        after={"status": "PENDING"},
        reason=str(reason or "人工重放")[:240])
    return task
