# -*- coding: utf-8 -*-
"""审批动作注册表（P0-02）：action_code 唯一白名单（代码级单一事实源）。

ApprovalRecord 的 action_code / entity_type / payload 必须来自本注册表：
  * API 层（apps/api/approval.py）：未知 action_code → 400，不写 ApprovalRecord、
    不写 AuditEvent；entity 目标对象必须存在、属于当前物业、状态允许申请。
  * Service 层（ApprovalService.request_approval）：重复校验注册表白名单、
    entity_type 一致性与 payload 字段白名单（双层防御，不只靠前端下拉框）。

新动作上线流程：实现执行服务 + 绑定申请/审批权限 + payload 白名单后，
在 ``_ACTIONS`` 登记；未实现的动作只能登记在 ``PENDING_ACTIONS``，不得注册。
"""
import re

from apps.core.api_contract import ApiError


# ---------------------------------------------------------------------------
# 敏感值识别：payload 不得携带身份证、密码、支付密钥类数据（P0-02 §5）
# ---------------------------------------------------------------------------
SENSITIVE_KEY_MARKERS = (
    "password", "passwd", "pwd", "secret", "token", "api_key", "apikey",
    "access_key", "private_key", "secret_key", "payment_key", "pay_key",
    "cvv", "cvc", "pin", "id_card", "idcard", "id_number", "identity_no",
    "credential", "session", "cookie",
    "密码", "口令", "身份证", "证件号", "密钥", "支付密钥", "卡号",
)

# 18 位身份证号（含末位 X）
_ID_CARD_PATTERN = re.compile(r"\d{17}[\dXx]")
# 16 位以上连续数字：银行卡/卡号类（金额、时间戳、短参考号不会命中）
_PAN_LIKE_PATTERN = re.compile(r"\d{16,}")
# 高熵随机串样式的支付密钥/私钥负载
_CREDENTIAL_PATTERN = re.compile(r"^[A-Za-z0-9+/=_-]{32,}$")


def _is_sensitive_key(key):
    normalized = str(key).strip().lower().replace("-", "_").replace(" ", "_")
    return any(marker in normalized for marker in SENSITIVE_KEY_MARKERS)


def _is_sensitive_value(value):
    if not isinstance(value, str):
        return False
    return bool(
        _ID_CARD_PATTERN.search(value)
        or _PAN_LIKE_PATTERN.search(value)
        or _CREDENTIAL_PATTERN.match(value)
    )


# ---------------------------------------------------------------------------
# 动作登记项
# ---------------------------------------------------------------------------
class ApprovalActionSpec:
    """单个审批动作的白名单登记项。"""

    __slots__ = (
        "action_code", "entity_type", "title", "request_permission",
        "approve_permission", "executor", "payload_fields", "expires_hours",
        "api_requestable", "validate_entity", "sod_rule",
    )

    def __init__(self, *, action_code, entity_type, title,
                 request_permission, approve_permission, executor,
                 payload_fields, expires_hours=48, api_requestable=True,
                 validate_entity=None, sod_rule="申请人≠审批人（Q-05 SoD）"):
        self.action_code = action_code
        self.entity_type = entity_type
        self.title = title
        self.request_permission = request_permission
        self.approve_permission = approve_permission
        self.executor = executor
        self.payload_fields = dict(payload_fields)
        self.expires_hours = expires_hours
        self.api_requestable = api_requestable
        self.validate_entity = validate_entity
        self.sod_rule = sod_rule

    def __repr__(self):
        return f"<ApprovalActionSpec {self.action_code} → {self.entity_type}>"


# ---------------------------------------------------------------------------
# entity 校验器（API 层调用）：对象存在 + 属当前物业 + 状态允许申请
# ---------------------------------------------------------------------------
def _int_or_none(entity_id):
    try:
        return int(str(entity_id))
    except (TypeError, ValueError):
        return None


def validate_refund_entity(property_obj, entity_id, requested_by):
    """退款审批：Refund 必须存在、属当前物业、待审批且为本人发起。

    通用审批面只允许补建/重放本人退款申请的审批记录；他人退款与已处理
    退款一律拒绝，避免绕过 BillingService.request_refund 的金额/余额校验。
    """
    from apps.billing.models import Refund
    refund = Refund.objects.select_related("payment").filter(
        pk=_int_or_none(entity_id)).first()
    if refund is None or refund.property_id != property_obj.pk:
        raise ApiError("RESOURCE_NOT_FOUND", "退款申请不存在或不属于当前物业。")
    if refund.requested_by_id != requested_by.pk:
        raise ApiError(
            "PERMISSION_DENIED", "只能为本人发起的退款申请提交审批。")
    if refund.status != Refund.Status.PENDING:
        raise ApiError("BUSINESS_CONFLICT", "退款申请已处理，不能重复提交审批。")
    return refund


def validate_reversal_entity(property_obj, entity_id, requested_by):
    """红字冲销审批：原凭证必须存在、属于当前物业且尚未被冲销。"""
    from apps.billing.models import ApprovalRecord, LedgerEntry
    entry = LedgerEntry.objects.filter(pk=_int_or_none(entity_id)).first()
    if entry is None or entry.property_id != property_obj.pk:
        raise ApiError("RESOURCE_NOT_FOUND", "账本凭证不存在或不属于当前物业。")
    if entry.entry_type == LedgerEntry.EntryType.REVERSAL:
        raise ApiError("BUSINESS_CONFLICT", "红字冲销凭证不能再次冲销。")
    if LedgerEntry.objects.filter(void_of=entry).exists():
        raise ApiError("BUSINESS_CONFLICT", "该账本凭证已经冲销。")
    existing = ApprovalRecord.objects.filter(
        property=property_obj, action_code="billing.reversal",
        entity_type="LedgerEntry", entity_id=str(entry.pk),
        status__in=(ApprovalRecord.Status.PENDING,
                    ApprovalRecord.Status.APPROVED),
    ).first()
    if existing is not None and existing.requested_by_id != requested_by.pk:
        raise ApiError("BUSINESS_CONFLICT", "该凭证已有其他财务人员发起的冲销申请。")
    return entry


def validate_business_date_entity(property_obj, entity_id, requested_by):
    """营业日重开审批：BusinessDate 必须属当前物业且已关账（对齐夜审语义）。"""
    from apps.core.models import BusinessDate
    bd = BusinessDate.objects.filter(
        pk=_int_or_none(entity_id), property=property_obj).first()
    if bd is None:
        raise ApiError("RESOURCE_NOT_FOUND", "营业日不存在或不属于当前物业。")
    if bd.status != BusinessDate.Status.CLOSED:
        raise ApiError("BUSINESS_CONFLICT", "仅已关账营业日可以申请重开。")
    return bd


def validate_staff_export_entity(property_obj, entity_id, requested_by):
    """StaffExport 为内部登记型实体（哈希幂等键，无数据库行），只做格式校验。"""
    text = str(entity_id or "").strip()
    if not text or len(text) > 64 or not re.fullmatch(r"[0-9a-f]+", text):
        raise ApiError("RESOURCE_NOT_FOUND", "导出申请标识不合法。")
    return text


# ---------------------------------------------------------------------------
# 唯一注册表
# ---------------------------------------------------------------------------
class ApprovalActionRegistry:
    """action_code → 登记项的唯一代码注册表（P0-02 §1/§2/§3）。"""

    _ACTIONS = {
        # 退款审批：执行链 BillingService.request_refund → complete_refund
        "billing.refund": ApprovalActionSpec(
            action_code="billing.refund",
            entity_type="Refund",
            title="退款审批",
            request_permission="billing.refund_request",
            approve_permission="billing.refund_approve",
            executor="apps.billing.services_v2.BillingService.complete_refund",
            payload_fields={"payment_id": 20, "amount": 16, "currency": 3},
            validate_entity=validate_refund_entity,
        ),
        # 红字冲销：财务申请并执行，值班经理/店长单独审批；执行时关联审批事实。
        "billing.reversal": ApprovalActionSpec(
            action_code="billing.reversal",
            entity_type="LedgerEntry",
            title="账本红字冲销审批",
            request_permission="billing.reverse_entry",
            approve_permission="billing.reversal_approve",
            executor="apps.billing.services_v2.BillingService.reverse_entry",
            payload_fields={},
            validate_entity=validate_reversal_entity,
            sod_rule="财务申请/执行；值班经理或店长审批；审批人≠执行人",
        ),
        # 营业日重开审批：执行链 NightAuditService.request_reopen → approve_reopen
        "night_audit.reopen": ApprovalActionSpec(
            action_code="night_audit.reopen",
            entity_type="BusinessDate",
            title="营业日重开审批",
            request_permission="hotelapp.business_date_reopen",
            approve_permission="hotelapp.business_date_reopen",
            executor="apps.billing.night_audit.NightAuditService.approve_reopen",
            payload_fields={"business_date": 10},
            validate_entity=validate_business_date_entity,
        ),
        # 敏感导出审批：仅旧专用通道（/export/staff/request|approve）可发起，
        # 不经通用 POST /approvals/request 开放（api_requestable=False）。
        # P0-04 统一授权迁移后，再评估开放或替换。
        "privacy.export_staff": ApprovalActionSpec(
            action_code="privacy.export_staff",
            entity_type="StaffExport",
            title="员工薪酬敏感导出审批（旧专用通道）",
            request_permission="hotelapp.export_pii",
            approve_permission="hotelapp.export_approve",
            executor="hotelapp.views.approve_staff_export",
            payload_fields={
                "module": 20, "contains_salary": 10,
                "format": 10, "requested_by": 20,
            },
            api_requestable=False,
            validate_entity=validate_staff_export_entity,
        ),
    }

    # 待实现动作登记区（P0-02 §1）：不得出现在 _ACTIONS，仅作台账。
    PENDING_ACTIONS = {
        "privacy.pii_export_unified":
            "统一审批面的 PII 批量导出审批待实现：旧通道 privacy.export_staff"
            "已按内部专用动作注册（api_requestable=False），P0-04 统一授权迁移"
            "后另行评估在通用审批面开放。",
    }

    @classmethod
    def get(cls, action_code):
        return cls._ACTIONS.get(str(action_code or "").strip())

    @classmethod
    def registered_codes(cls):
        return tuple(sorted(cls._ACTIONS))

    @classmethod
    def api_requestable_codes(cls):
        return tuple(sorted(
            code for code, spec in cls._ACTIONS.items() if spec.api_requestable))

    @classmethod
    def approver_permissions(cls):
        """全部已注册动作的审批权限码（用于列表可见范围判定）。"""
        return tuple(sorted({spec.approve_permission for spec in cls._ACTIONS.values()}))

    @classmethod
    def validate_entity_type(cls, spec, entity_type):
        """entity_type 必须与登记项一致（双层校验的 Service 侧共用）。"""
        if str(entity_type or "").strip() != spec.entity_type:
            raise ValueError(
                f"审批动作 {spec.action_code} 的目标实体类型必须为 "
                f"{spec.entity_type}。")

    @classmethod
    def validate_payload(cls, spec, payload):
        """payload 字段白名单 + 长度限制 + 敏感值拒绝；返回清洗后的副本。"""
        if payload is None:
            return None
        if not isinstance(payload, dict):
            raise ValueError("payload 必须是 JSON 对象。")
        if len(payload) > 20:
            raise ValueError("payload 字段数量过多。")
        cleaned = {}
        for key, value in payload.items():
            key = str(key)
            if key not in spec.payload_fields:
                raise ValueError(f"payload 字段 {key} 不在该审批动作的白名单内。")
            if _is_sensitive_key(key):
                raise ValueError(f"payload 字段 {key} 属于敏感信息，禁止写入审批快照。")
            if value is None:
                cleaned[key] = None
                continue
            if isinstance(value, (dict, list, tuple, set)):
                raise ValueError(f"payload 字段 {key} 不支持嵌套结构。")
            if isinstance(value, float):
                raise ValueError(f"payload 字段 {key} 必须使用字符串/整数，不接受浮点数。")
            text = str(value)
            max_len = spec.payload_fields[key]
            if len(text) > max_len:
                raise ValueError(f"payload 字段 {key} 长度不能超过 {max_len} 个字符。")
            if _is_sensitive_value(text):
                raise ValueError(f"payload 字段 {key} 的值疑似身份证/卡号/密钥，禁止写入审批快照。")
            cleaned[key] = value
        return cleaned
