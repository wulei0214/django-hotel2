# -*- coding: utf-8 -*-
"""PMS2 OpenAPI 3.1 契约（方案 §9.8：API 契约与 Swagger/OpenAPI 交付）。

契约描述表 ``CONTRACTS`` 以视图源码为准逐条核实（apps/api/views.py、
front_desk.py、guest_center.py、night_audit.py、approval.py、
reservation_center.py、staff_center.py 与相关领域服务），由
:func:`build_openapi` 渲染为 OpenAPI 3.1 文档，经 /api/v1/openapi.json 暴露。

扩展字段（x-）：
- x-contract-status: verified（有 HTTP 级测试 + 字段逐项核实）/ partial（字段已按源码
  核实但尚缺端点级 HTTP 测试）/ planned（规划未实现；当前不存在）。
- x-required-permission: 与视图装饰器 / AuthorizationService 校验一致的权限码；
  匿名端点不声明该字段而以 ``security: []`` 显式标注匿名。
- x-authentication: Django Session Cookie / 匿名 + 业务凭据（Booking Access Token）。
- x-rate-limit: §6.3 公开面限流阈值（与 apps.core.rate_limit.DEFAULT_LIMITS 对齐）。
- x-idempotency: Idempotency-Key 幂等语义（含必填与重放行为）。
- x-audit-events: 该端点会产生/消费的 AuditEvent 动作。
- x-booking-access-token: 公开预订 Booking Access Token 凭据说明。
- x-no-request-body: POST 但视图不读取请求体的端点标记。

响应封套：
- 新模块（api_contract）：成功 ``{data, meta}``；失败 ``{error{code,message}, meta}``。
- 前台接待模块：成功 ``{data, ...标志位}``；失败 ``{error{code, message}}``。
- 公开预订旧契约：错误为 ``{error: "<message>"}``（字符串），429 附 ``retry_after``。
"""
import re

from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_GET

BASE = "/api/v1"

TAGS = ["公开预订", "支付", "前台接待", "夜审与紧急访问", "住客中心", "敏感数据",
        "房态库存", "房价管理", "清洁与维修", "财务", "审批", "预订管理", "审计",
        "员工中心", "会员中心", "报表分析", "通知与待办", "文档"]

COOKIE_SECURITY = [{"PMS2Session": []}]
CSRF_PARAM = {"name": "X-CSRFToken", "in": "header", "required": True,
              "description": "Django CSRF 令牌（CSRF_USE_SESSIONS=True）；所有 Session/公开写操作必须携带。",
              "schema": {"type": "string"}}

REF = "#/components/schemas/{}"


def _ref(name):
    return {"$ref": REF.format(name)}


# ---------------------------------------------------------------------------
# Schema 便捷构造器
# ---------------------------------------------------------------------------
def s_str(desc="", **kw):
    return {"type": "string", "description": desc, **kw}


def s_int(desc="", **kw):
    return {"type": "integer", "description": desc, **kw}


def s_num(desc="", **kw):
    return {"type": "number", "description": desc, **kw}


def s_bool(desc=""):
    return {"type": "boolean", "description": desc}


def s_null(desc=""):
    return {"type": "null", "description": desc}


def s_arr(items, desc=""):
    return {"type": "array", "description": desc, "items": items}


def s_obj(props=None, required=(), desc="", additional=True):
    schema = {"type": "object", "description": desc,
              "properties": props or {}, "additionalProperties": additional}
    if required:
        schema["required"] = list(required)
    return schema


def s_nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


# ---------------------------------------------------------------------------
# 参数构造器
# ---------------------------------------------------------------------------
def P(name, desc, typ="string", location="path", required=True, example=None,
      fmt=None):
    schema = {"type": typ}
    if example is not None:
        schema["example"] = example
    if fmt is not None:
        schema["format"] = fmt
    return {"name": name, "in": location, "required": required,
            "description": desc, "schema": schema}


def Q(name, desc, typ="string", required=False, example=None, fmt=None):
    return P(name, desc, typ, "query", required, example, fmt)


def PQ(page_default=20, size_max=100):
    """统一分页参数（front_desk._page_args / api_contract.paginate 同口径）。"""
    return [
        Q("page", f"页码，从 1 开始，默认 1。", "integer", False, 1),
        Q("page_size", f"每页数量，默认 {page_default}，最大 {size_max}。", "integer", False, page_default),
    ]


def _path_param(name, desc):
    typ = "integer" if name.endswith("_id") else "string"
    return P(name, desc, typ, "path")


# ---------------------------------------------------------------------------
# 共享组件 schema
# ---------------------------------------------------------------------------
COMPONENTS = {
    "ResponseMeta": s_obj({
        "request_id": s_str("请求追踪 ID（request.request_id 或 16 位随机 hex）。"),
        "source_system": s_str("来源系统标识，固定 PMS2。"),
        "generated_at": s_str("响应生成时间 ISO-8601。", format="date-time"),
        "pagination": s_nullable(_ref("PaginationMeta")),
    }, required=["request_id", "source_system", "generated_at"]),
    "PaginationMeta": s_obj({
        "page": s_int(), "page_size": s_int(), "total": s_int(),
        "has_next": s_bool(), "has_previous": s_bool(),
    }, desc="统一分页元数据；部分列表会附加 date/source_system/read_only 等附加字段。"),
    "ErrorDetail": s_obj({
        "code": s_str("稳定错误码：VALIDATION_ERROR/AUTHENTICATION_REQUIRED/PERMISSION_DENIED/"
                      "RESOURCE_NOT_FOUND/BUSINESS_CONFLICT/RATE_LIMITED/"
                      "PROPERTY_UNAVAILABLE/EMPLOYEE_MEMBERSHIP_REQUIRED/INTERNAL_ERROR。"),
        "message": s_str("人类可读错误信息（中文）。"),
        "field_errors": s_nullable(s_obj(desc="字段级校验错误映射。")),
        "retryable": s_nullable(s_bool("是否建议原样重试。")),
    }, required=["code", "message"], desc="统一错误明细（apps.core.api_contract.ApiError）。"),
    "ApiErrorEnvelope": s_obj({
        "error": _ref("ErrorDetail"),
        "meta": s_nullable(_ref("ResponseMeta")),
    }, required=["error"], desc="员工端统一错误封套；api_contract 模块带 meta，前台接待模块仅 error 节点。"),
    "PublicApiError": s_obj({
        "error": s_str("错误信息（公开预订旧契约：error 为字符串）。"),
    }, required=["error"], desc="公开预订面错误结构（apps/api/views.py）。"),
    "PublicRateLimitError": s_obj({
        "error": s_str("固定文案：请求过于频繁，请稍后再试。"),
        "retry_after": s_int("建议等待秒数；同时写入 Retry-After 响应头。"),
    }, required=["error"], desc="公开面 429 限流响应（§6.3）。"),

    "NightlyRate": s_obj({
        "date": s_str("住宿日期 YYYY-MM-DD。", format="date"),
        "price": s_str("每晚价格（字符串十进制，两位小数）。"),
        "currency": s_str("币种，如 CNY。"),
    }),
    "RoomTypeAvailability": s_obj({
        "room_type_id": s_int("房型 ID。"),
        "room_type_name": s_str("房型名称。"),
        "rate_plan_id": s_int("匹配到的房价计划 ID。"),
        "available_count": s_int("区间内每天可售量的最小值；不可售时为 0。"),
        "total_amount": s_nullable(s_str("住宿期内每间总价（字符串十进制）；不可售为 null。")),
        "nightly_details": s_arr(_ref("NightlyRate"), "逐晚价格明细；不可售为空数组。"),
    }, desc="get_room_type_availability 返回结构（apps/inventory/services.py）。"),

    "BookingCreatedResult": s_obj({
        "booking_no": s_str("预订号。", example="R202611050001"),
        "status": s_str("预订状态（PENDING 等）。"),
        "check_in": s_str("入住日期。", format="date"),
        "check_out": s_str("离店日期。", format="date"),
        "room_count": s_int("房间数。"),
        "total_amount": s_str("订单总额（字符串十进制）。"),
        "currency": s_str("币种。"),
        "expires_at": s_nullable(s_str("待支付过期时间。", format="date-time")),
        "created": s_bool("本次是否为新创建；幂等重放为 false。"),
        "access_token": s_str("Booking Access Token，仅首次创建返回一次，顾客须自行保存。"),
    }, desc="创建预订响应；access_token 明文仅出现一次。"),
    "BookingAccessTokenDetail": s_obj({
        "booking_no": s_str("预订号。"),
        "status": s_str("预订状态。"),
        "guest_name": s_str("脱敏住客姓名（首字符 + ***）。"),
        "check_in": s_str("入住日期。", format="date"),
        "check_out": s_str("离店日期。", format="date"),
        "rooms": s_int("房间数。"),
        "total_amount": s_str("订单总额。"),
        "currency": s_str("币种。"),
        "payments": s_arr(s_obj({
            "amount": s_str("支付金额。"),
            "status": s_str("支付状态。"),
            "paid_at": s_nullable(s_str("支付时间。", format="date-time")),
        }), "支付摘要；不返回完整手机号/证件号/provider_ref/内部流水。"),
    }, desc="§6.5 字段白名单脱敏后的顾客预订视图。"),
    "BookingCancelResult": s_obj({
        "booking_no": s_str("预订号。"),
        "status": s_str("取消后的预订状态。"),
        "changed": s_bool("本次是否实际发生状态变更；重复取消幂等返回 false。"),
    }),
    "PaymentIntentResult": s_obj({
        "provider": s_str("固定 mock。"),
        "provider_ref": s_str("模拟支付流水号。"),
        "status": s_str("支付单状态。"),
        "amount": s_str("金额（字符串十进制）。"),
        "currency": s_str("币种。"),
        "message": s_str("本地模拟支付提示文案。"),
        "mock_callback_example": s_obj({
            "url": s_str(), "payload": s_obj(), "signature": s_str(),
            "note": s_str("仅本地 dev/test 返回该样例。"),
        }, desc="dev/test 环境附带的模拟回调样例。"),
    }),
    "MockCallbackResult": s_obj({
        "ok": s_bool(), "duplicate": s_bool("是否重复回调。"),
        "expired": s_bool("支付单是否已过期。"),
        "payment_status": s_str("支付单状态。"),
        "booking_no": s_str("预订号。"),
        "reservation_status": s_str("预订状态。"),
    }),

    "NightAuditPreviewRow": s_obj({
        "stay_id": s_int("在住 Stay ID。"),
        "booking_no": s_str("预订号。"),
        "room_number": s_str("房号。"),
        "amount": s_str("当日房价（字符串十进制，两位小数）。"),
        "currency": s_str("币种。"),
        "already_posted": s_bool("当日房费是否已入账。"),
        "reference": s_str("过账引用（night-audit:{property}:{date}:stay:{id}）。"),
    }),
    "NightAuditPreviewResult": s_obj({
        "business_date": s_str("营业日期。", format="date"),
        "business_date_status": s_str("营业日状态。"),
        "stays": s_arr(_ref("NightAuditPreviewRow")),
        "stay_count": s_int(), "charge_count": s_int("待过账房费条数。"),
        "already_posted_count": s_int(), "charge_total": s_str("待过账合计。"),
        "currency": s_str(), "no_show_review_candidates": s_int("未到处理候选数（只列示不自动处理）。"),
        "note": s_str("No-show 政策口径说明。"),
    }, desc="NightAuditService.preview 返回结构。"),
    "NightAuditRunResult": s_obj({
        "run_id": s_int("夜审运行记录 ID。"), "status": s_str("夜审状态。"),
        "summary": s_obj(desc="夜审汇总（由 NightAuditService 生成）。"),
        "created": s_bool("本次是否新执行；重复执行返回上次结果。"),
    }),
    "ReopenRequestResult": s_obj({
        "approval_id": s_int("生成的审批记录 ID。"), "status": s_str("审批状态。"),
        "business_date_id": s_int(), "created": s_bool(),
    }),
    "ReopenApproveResult": s_obj({
        "business_date_id": s_int(), "business_date": s_str("营业日期。", format="date"),
        "status": s_str("营业日状态（重开后为 OPEN）。"),
        "reopened_by": s_nullable(s_int("执行重开的用户 ID。")),
    }),
    "BreakGlassRequestResult": s_obj({
        "break_glass_id": s_int("紧急授权记录 ID。"),
        "business_date_id": s_int(), "status": s_str("授权状态。"),
        "expires_at": s_str("十分钟授权到期时间。", format="date-time"),
        "action": s_str("授权动作码。"),
        "message": s_str("操作提示（须立即执行一次并接受事后复核）。"),
    }),
    "BreakGlassExecuteResult": s_obj({
        "business_date_id": s_int(), "business_date": s_str("营业日期。", format="date"),
        "status": s_str("营业日状态。"),
        "reopened_by": s_nullable(s_int()),
        "review_required": s_bool("固定 true：必须由另一账号事后复核。"),
    }),
    "BreakGlassReviewResult": s_obj({
        "break_glass_id": s_int(), "status": s_str(),
        "review_decision": s_str("CONFIRMED / REJECTED。"),
        "reviewed_by": s_nullable(s_int()),
        "reviewed_at": s_str("复核时间。", format="date-time"),
    }),

    "NightAuditRunDto": s_obj({
        "id": s_int(), "business_date_id": s_int(),
        "business_date": s_str("营业日期。", format="date"),
        "business_date_status": s_str("营业日状态。"),
        "status": s_str("RUNNING/SUCCESS/FAILED。"),
        "status_label": s_str("运行状态中文标签。"),
        "started_at": s_str("开始时间。", format="date-time"),
        "finished_at": s_nullable(s_str("完成时间。", format="date-time")),
        "duration_seconds": s_nullable(s_int("执行耗时秒（未完成为 null）。")),
        "executed_by_id": s_nullable(s_int("执行人 ID。")),
        "summary": s_nullable(s_obj(desc="成功运行的结果摘要（在住/过租/未到计数）。")),
        "error_message": s_str("失败原因（列表截断 300 字符，详情最长 4000）。"),
    }, desc="夜审运行记录（apps.core.models.NightAuditRun）。"),
    "BusinessDateDto": s_obj({
        "id": s_int(), "business_date": s_str("营业日期。", format="date"),
        "status": s_str("OPEN/CLOSED/REOPENED。"),
        "status_label": s_str("状态中文标签。"),
        "opened_at": s_str("开启时间。", format="date-time"),
        "closed_at": s_nullable(s_str("关账时间。", format="date-time")),
        "closed_by_id": s_nullable(s_int()),
        "reopened_at": s_nullable(s_str("重开时间。", format="date-time")),
        "reopened_by_id": s_nullable(s_int()),
        "reopen_reason": s_str("重开原因（无则为空串）。"),
        "latest_run": s_nullable(_ref("NightAuditRunDto")),
    }, desc="营业日记录（apps.core.models.BusinessDate）。"),
    "BusinessDateCurrentResult": s_obj({
        "id": s_nullable(s_int("营业日记录 ID；未建立记录为 null。")),
        "business_date": s_str("按物业时区解析的当前营业日期。", format="date"),
        "exists": s_bool("是否已存在营业日记录。"),
        "status": s_str("状态；无记录时按 OPEN 口径返回。"),
        "status_label": s_str("状态中文标签。"),
    }),
    "BusinessDateDetail": s_obj({
        "id": s_int(), "business_date": s_str("营业日期。", format="date"),
        "status": s_str("OPEN/CLOSED/REOPENED。"),
        "status_label": s_str("状态中文标签。"),
        "opened_at": s_str("开启时间。", format="date-time"),
        "closed_at": s_nullable(s_str("关账时间。", format="date-time")),
        "closed_by_id": s_nullable(s_int()),
        "reopened_at": s_nullable(s_str("重开时间。", format="date-time")),
        "reopened_by_id": s_nullable(s_int()),
        "reopen_reason": s_str("重开原因（无则为空串）。"),
        "latest_run": s_nullable(_ref("NightAuditRunDto")),
        "runs": s_arr(_ref("NightAuditRunDto"), "该营业日全部夜审运行（新→旧）。"),
        "latest_success_summary": s_nullable(s_obj(desc="最近一次成功夜审的摘要。")),
        "approvals": s_arr(s_obj({
            "id": s_int(), "action_code": s_str("动作码（night_audit.reopen）。"),
            "status": s_str("审批状态。"),
            "requested_by_id": s_int(), "approved_by_id": s_nullable(s_int()),
            "reason": s_str("申请原因。"),
            "created_at": s_str("申请时间。", format="date-time"),
        }), "重开审批记录（最近 20 条）。"),
        "no_show_review_candidates": s_int("该营业日未到处理候选数（只列示不自动处理）。"),
        "ledger": s_obj({
            "business_date": s_str("", format="date"),
            "currencies": s_obj(desc="按币种分组：entry_count/charge_total/credit_total。"),
        }, desc="该营业日已入账凭证汇总。"),
        "room_status_summary": s_obj({
            "total_rooms": s_int(),
            "occupancy": s_obj(desc="在住状态计数映射。"),
            "housekeeping": s_obj(desc="清洁状态计数映射。"),
            "maintenance": s_obj(desc="维修状态计数映射。"),
            "abnormal": s_arr(s_obj({
                "maintenance_status": s_str("非正常维修状态。"),
                "room_count": s_int("房间数。"),
            }), "维修停用/停售房间分组。"),
        }, desc="物业当前异常房态汇总。"),
    }, desc="营业日详情（§4.2：运行历史 + 重开审批 + 过租/未到/异常房态汇总）。"),
    "BreakGlassRecordDto": s_obj({
        "id": s_int(), "business_date_id": s_int(),
        "business_date": s_str("营业日期。", format="date"),
        "status": s_str("GRANTED/USED/EXPIRED/REVIEWED。"),
        "status_label": s_str("状态中文标签。"),
        "requested_by_id": s_int("紧急操作人 ID。"),
        "reason": s_str("紧急原因。"),
        "created_at": s_str("申请时间。", format="date-time"),
        "expires_at": s_str("授权失效时间。", format="date-time"),
        "used_at": s_nullable(s_str("执行时间。", format="date-time")),
        "reviewed_by_id": s_nullable(s_int()),
        "reviewed_at": s_nullable(s_str("复核时间。", format="date-time")),
        "review_decision": s_str("CONFIRMED/REJECTED 或空。"),
        "review_decision_label": s_str("复核结论中文标签。"),
        "review_reason": s_str("复核说明。"),
        "review_pending": s_bool("status=USED 即待独立复核。"),
        "is_expired_grant": s_bool("GRANTED 但已过 expires_at。"),
    }, desc="紧急重开授权记录（apps.core.models.BreakGlassRecord）。"),

    "FolioSummaryDto": s_obj({
        "folio_id": s_nullable(s_int("账单 ID；未开账为 null。")),
        "status": s_str("账单状态；未开账为 NOT_OPENED。"),
        "status_label": s_str("状态中文标签。"),
        "charges": s_str("借方合计。"), "credits": s_str("贷方合计。"),
        "balance": s_str("余额。"),
        "pending_payment_count": s_int("处理中支付笔数。"),
        "currency": s_str(), "currency_label": s_str(),
    }),
    "FrontDeskFolioSummaryDto": s_obj({
        "folio_id": s_nullable(s_int("主账单 ID；未开账为 null。")),
        "status": s_str("主账单状态；未开账为 NOT_OPENED。"),
        "status_label": s_str("主账单状态中文标签。"),
        "charges": s_nullable(s_str("所有 OPEN Folio 借方合计；币种不一致时为 null。")),
        "credits": s_nullable(s_str("所有 OPEN Folio 贷方合计；币种不一致时为 null。")),
        "balance": s_nullable(s_str("所有 OPEN Folio 带符号余额；币种不一致时为 null。")),
        "pending_payment_count": s_int("所有 OPEN Folio 处理中支付笔数。"),
        "currency": s_str(), "currency_label": s_str(),
        "folio_count": s_int("预订关联的全部账单数，含历史已结账单。"),
        "open_folio_count": s_int("参与当前余额计算的未结账单数。"),
        "currency_mismatch": s_bool("未结账单币种与预订币种不一致时为 true；金额字段不合并。"),
    }),
    "ReservationRoomSummary": s_obj({
        "reservation_room_id": s_int(),
        "booking_no": s_str(),
        "reservation_status": s_str(), "reservation_status_label": s_str(),
        "source": s_str(), "source_label": s_str(),
        "guest": s_obj({
            "id": s_int(), "full_name": s_str(), "phone_masked": s_str("脱敏手机号。"),
        }),
        "room_type": s_obj({"id": s_int(), "name": s_str()}),
        "assigned_room": s_nullable(s_obj({
            "id": s_int(), "number": s_str(), "floor": s_str(),
        }, desc="已分配房间；未排房为 null。")),
        "check_in": s_str("", format="date"), "check_out": s_str("", format="date"),
        "nights": s_int(),
        "adults": s_int(), "children": s_int(),
        "amount": s_str("该明细房费。"),
        "folio_summary": _ref("FrontDeskFolioSummaryDto"),
        "stay_status_label": s_str("该房间实际入住状态中文标签；已确认但未办理入住显示待办理入住。"),
        "stay": s_nullable(s_obj({
            "id": s_int(), "status": s_str(), "status_label": s_str(),
            "actual_check_in": s_nullable(s_str("", format="date-time")),
            "room_segments": s_arr(s_obj({
                "room_number": s_str(), "started_at": s_str("", format="date-time"),
                "ended_at": s_nullable(s_str("", format="date-time")),
                "reason": s_nullable(s_str("换房原因。")),
            }), "换房段记录。"),
        }, desc="在住 Stay 概要；未入住为 null。")),
        "no_show": s_nullable(s_obj({
            "id": s_int(), "reason": s_str(),
            "processed_at": s_str("", format="date-time"),
            "released_nights": s_int("释放的库存晚数。"),
        }, desc="No-show 记录；无则为 null。")),
        "source_system": s_str("固定 PMS2。"),
    }, desc="_reservation_room_dto（apps/api/front_desk.py）。"),
    "FrontDeskReservationListItem": s_obj({
        "booking_no": s_str(), "status": s_str(), "status_label": s_str(),
        "source": s_str(), "source_label": s_str(),
        "guest": s_obj({"id": s_int(), "full_name": s_str(), "phone_masked": s_str()}),
        "check_in": s_str("", format="date"), "check_out": s_str("", format="date"),
        "nights": s_int(), "room_count": s_int(),
        "adult_count": s_int(), "child_count": s_int(),
        "total_amount": s_str(), "currency": s_str(),
        "rooms": s_arr(s_obj({
            "id": s_int("明细 ID"), "room_type": s_str("房型名"),
            "assigned_room": s_nullable(s_str("已分配房号；未排房为 null。")),
        })),
        "folio_summary": _ref("FrontDeskFolioSummaryDto"),
        "source_system": s_str(),
    }),
    "FrontDeskReservationDetail": s_obj({
        "booking_no": s_str(), "status": s_str(), "status_label": s_str(),
        "source": s_str(), "source_label": s_str(),
        "guest": s_obj({"id": s_int(), "full_name": s_str(), "phone_masked": s_str()}),
        "check_in": s_str("", format="date"), "check_out": s_str("", format="date"),
        "nights": s_int(), "room_count": s_int(),
        "adult_count": s_int(), "child_count": s_int(),
        "total_amount": s_str(), "currency": s_str(), "currency_label": s_str(),
        "rooms": s_arr(_ref("ReservationRoomSummary")),
        "folio_summary": _ref("FolioSummaryDto"),
        "timeline": s_obj({
            "created_at": s_str("", format="date-time"),
            "updated_at": s_str("", format="date-time"),
            "stays": s_arr(s_obj({
                "id": s_int(), "status": s_str(), "status_label": s_str(),
                "created_at": s_str("", format="date-time"),
                "actual_check_in": s_nullable(s_str("", format="date-time")),
                "actual_check_out": s_nullable(s_str("", format="date-time")),
            })),
        }),
        "source_system": s_str(),
        "allowed_actions": s_arr(s_str(), "当前用户可用操作：view_detail/operate/mark_no_show/request_refund/approve_refund。"),
    }),
    "FrontDeskRoomRow": s_obj({
        "id": s_int(), "number": s_str(), "floor": s_str(),
        "room_type": s_obj({"id": s_int(), "name": s_str()}),
        "occupancy_status": s_str(), "occupancy_label": s_str(),
        "housekeeping_status": s_str(), "housekeeping_label": s_str(),
        "maintenance_status": s_str(), "maintenance_label": s_str(),
        "is_ready": s_bool("is_sellable_now：可售即洁净+空闲+无维修。"),
    }),
    "AssignableRoom": s_obj({
        "id": s_int(), "number": s_str(), "floor": s_str(), "room_type": s_str(),
    }),
    "FolioEntryDto": s_obj({
        "id": s_int(), "entry_type": s_str(), "amount": s_str(),
        "currency": s_str(), "business_date": s_str("", format="date"),
        "reference": s_nullable(s_str()), "description": s_nullable(s_str()),
        "created_at": s_str("", format="date-time"),
    }),
    "RefundDto": s_obj({
        "id": s_int(), "amount": s_str(), "reason": s_str(),
        "status": s_str(), "status_label": s_str(),
        "provider_ref": s_nullable(s_str()),
    }),
    "FolioPaymentDto": s_obj({
        "id": s_int(), "provider": s_str(), "provider_ref": s_nullable(s_str()),
        "method": s_str(), "amount": s_str(), "status": s_str(), "status_label": s_str(),
        "paid_at": s_nullable(s_str("", format="date-time")),
        "refunds": s_arr(_ref("RefundDto")),
    }),
    "FolioDetailData": s_obj({
        "folio": s_nullable(s_obj(desc="未开账时为 null 键；已开账时视图不返回该键。")),
        "summary": s_nullable(_ref("FolioSummaryDto")),
        "entries": s_arr(_ref("FolioEntryDto")),
        "payments": s_arr(_ref("FolioPaymentDto")),
        "allowed_actions": s_arr(s_str(), "request_refund / approve_refund（按权限）。"),
    }, desc="未开账时 folio 为 null、summary 为空账摘要且 entries/payments 为空。"),
    "LegacyReservationDto": s_obj({
        "booking_no": s_str("LEGACY-{id} 形式的展示编号。"),
        "legacy_id": s_int("旧 hotelapp.Accommodation 主键。"),
        "status": s_str("旧系统状态原文。"), "status_label": s_str(),
        "source": s_str("固定 LEGACY。"), "source_label": s_str("历史 hotelapp 订单。"),
        "guest": s_obj({"id": s_nullable(s_int()), "full_name": s_str(), "phone_masked": s_str()}),
        "check_in": s_nullable(s_str("", format="date")),
        "check_out": s_nullable(s_str("", format="date")),
        "room_count": s_int("固定 1（旧模型单房间）。"),
        "adult_count": s_nullable(s_int()), "child_count": s_nullable(s_int()),
        "total_amount": s_nullable(s_str("旧单无总额字段，恒为 null。")),
        "currency": s_str("固定 CNY。"),
        "rooms": s_arr(s_obj({
            "id": s_nullable(s_int()),
            "room_type": s_nullable(s_obj({"name": s_str()})),
            "assigned_room": s_nullable(s_obj({"number": s_str()})),
        })),
        "room_type": s_nullable(s_obj({"name": s_str()})),
        "assigned_room": s_nullable(s_obj({"number": s_str()})),
        "legacy_paid_amount": s_nullable(s_str("旧收款金额。")),
        "legacy_deposit_amount": s_nullable(s_str("旧押金金额。")),
        "folio_summary": s_obj({"status": s_str("LEGACY_ARCHIVE"), "currency": s_str()}),
        "source_system": s_str("LEGACY"),
        "read_only": s_bool("固定 true：历史记录只读。"),
    }),
    "LegacyFolioEntryDto": s_obj({
        "id": s_int(), "entry_type": s_str("旧类型字段原文。"),
        "amount_raw": s_str("旧金额原文（不参与 PMS2 对账）。"),
        "business_date": s_nullable(s_str("", format="date")),
        "description": s_str("旧事由字段。"),
        "operator": s_str("旧经手人字段。"),
        "source_system": s_str("LEGACY"),
    }),

    "GuestDto": s_obj({
        "id": s_int(), "full_name": s_str(),
        "phone_masked": s_nullable(s_str("主手机号脱敏。")),
        "email": s_nullable(s_str()),
        "member": s_nullable(s_obj({
            "member_no": s_str(), "level": s_str(), "level_label": s_str(),
            "status": s_str(), "status_label": s_str(),
        }, desc="当前物业有效会员关系；无则为 null。")),
        "current_stay": s_nullable(s_obj({
            "room_number": s_str(), "check_in": s_str("", format="date"),
        }, desc="当前在住房间；无则为 null。")),
        "source_system": s_str(),
        "created_at": s_str("", format="date-time"),
    }),
    "GuestDetailDto": s_obj({
        "documents": s_arr(s_obj({
            "id": s_int(), "doc_type": s_str(), "masked": s_str("证件号掩码。"),
            "is_primary": s_bool(), "verified": s_bool("是否已人工核验。"),
        })),
        "phones": s_arr(s_obj({
            "phone": s_str("脱敏手机号。"), "type": s_str(), "is_primary": s_bool(),
        })),
        "notes": s_nullable(s_str()),
        "reservations": s_arr(s_obj({
            "booking_no": s_str(), "status": s_str(), "status_label": s_str(),
            "check_in": s_str(), "check_out": s_str(),
            "total_amount": s_str(), "rooms": s_int(),
        }), "最近 10 条预订。"),
    }, desc="在 GuestDto 基础上附加 detail 字段（Guest 360）。"),
    "RoomStatusRow": s_obj({
        "id": s_int(), "number": s_str(), "floor": s_nullable(s_str()),
        "room_type": s_obj({"id": s_int(), "code": s_str(), "name": s_str()}),
        "occupancy_status": s_str(), "occupancy_label": s_str(),
        "housekeeping_status": s_str(), "housekeeping_label": s_str(),
        "maintenance_status": s_str(), "maintenance_label": s_str(),
        "is_ready": s_bool(), "source_system": s_str(),
    }),
    "RoomTypeRow": s_obj({
        "id": s_int(), "code": s_str(), "name": s_str(),
        "capacity": s_int("核定入住人数。"),
        "bed_type": s_nullable(s_str()), "is_active": s_bool(),
    }),
    "InventoryRow": s_obj({
        "room_type": s_obj({"id": s_int(), "code": s_str(), "name": s_str()}),
        "stay_date": s_str("", format="date"),
        "total_rooms": s_int(), "held": s_int(), "sold": s_int(),
        "out_of_service": s_int(), "available_count": s_int(),
        "stop_sell": s_bool(),
    }),
    "RateRow": s_obj({
        "room_type": s_obj({"id": s_int(), "code": s_str(), "name": s_str()}),
        "rate_plan": s_obj({"id": s_int(), "code": s_str(), "name": s_str()}),
        "stay_date": s_str("", format="date"),
        "price": s_str(), "currency": s_str(), "currency_label": s_str(),
        "closed": s_bool(),
    }),
    "RateCalendarData": s_obj({
        "inventory": s_arr(_ref("InventoryRow")),
        "rates": s_arr(_ref("RateRow")),
    }),
    "FinancialSummaryData": s_obj({
        "total_revenue": s_str("近 7 天收入合计。"),
        "total_expense": s_str("近 7 天支出合计。"),
        "net": s_str("净额。"),
        "payment_count": s_int(), "payment_total": s_str(),
        "refund_count": s_int(), "refund_total": s_str(),
        "entry_count": s_int("LedgerEntry 条数。"),
        "period": s_obj({"start": s_str("", format="date"), "end": s_str("", format="date")}),
        "detail_available": s_bool("hotelapp.finance_access 决定能否查看明细。"),
        "note": s_nullable(s_str("值班经理无明细权限时的提示。")),
    }, desc="BillingService.financial_summary（近 7 天窗口）。"),
    "DailyRateAdjustResult": s_obj({
        "daily_rate_id": s_int(), "room_type_id": s_int(), "rate_plan_id": s_int(),
        "stay_date": s_str("", format="date"),
        "price": s_str("调整后价格。"), "currency": s_str(),
        "changed": s_bool("同价不改时为 false。"),
    }),
    "ApprovalRecordDto": s_obj({
        "id": s_int(), "action_code": s_str(),
        "entity_type": s_str(), "entity_id": s_str(),
        "status": s_str("PENDING/APPROVED/REJECTED 等。"),
        "requested_by": s_nullable(s_str("申请人用户名。")),
        "approved_by": s_nullable(s_str("审批人用户名。")),
        "reason": s_str(), "created_at": s_str("", format="date-time"),
        "approved_at": s_nullable(s_str("", format="date-time")),
    }),
    "MgmtReservationListItem": s_obj({
        "id": s_int(), "booking_no": s_str(),
        "guest": s_obj({"id": s_int(), "full_name": s_str()}),
        "status": s_str(), "source": s_str(),
        "check_in": s_str(), "check_out": s_str(),
        "room_count": s_int(), "total_amount": s_str(),
        "currency": s_str(), "source_system": s_str(),
    }),
    "MgmtReservationDetail": s_obj({
        "id": s_int(), "booking_no": s_str(),
        "guest": s_obj({"id": s_int(), "full_name": s_str()}),
        "status": s_str(), "source": s_str(),
        "check_in": s_str(), "check_out": s_str(),
        "room_count": s_int(), "adult_count": s_int(), "child_count": s_int(),
        "total_amount": s_str(), "currency": s_str(),
        "rooms": s_arr(s_obj({
            "id": s_int(), "room_type": s_str(),
            "assigned_room": s_nullable(s_str()),
            "check_in": s_str(), "check_out": s_str(), "amount": s_str(),
        })),
        "source_system": s_str(),
    }),
    "AuditEventDto": s_obj({
        "id": s_int(), "action": s_str("审计动作码。"),
        "entity_type": s_str(), "entity_id": s_str(),
        "actor": s_str("操作人用户名；系统动作为 system。"),
        "reason": s_nullable(s_str()),
        "before": s_nullable(s_obj(desc="变更前快照。")),
        "after": s_nullable(s_obj(desc="变更后快照。")),
        "created_at": s_str("", format="date-time"),
    }),
    "StaffDto": s_obj({
        "id": s_int("StaffProfile ID。"), "full_name": s_str(),
        "phone_masked": s_nullable(s_str()),
        "employee_no": s_nullable(s_str("当前物业工号。")),
        "position_code": s_nullable(s_str("岗位代码。")),
        "employment_status": s_str(), "employment_status_label": s_str(),
        "membership_status": s_nullable(s_str("当前物业任职状态。")),
        "membership_status_label": s_nullable(s_str()),
        "account": s_nullable(s_obj({
            "username": s_str(), "is_active": s_bool(),
            "password_pending": s_bool("unusable password = 邀请待激活账号（激活前不可登录）。"),
            "last_login": s_nullable(s_str("", format="date-time")),
        }, desc="关联登录账号；无则为 null。")),
        "invitation": s_nullable(s_obj({
            "id": s_int("邀请记录 ID。"),
            "status": s_str("邀请状态。"),
            "status_label": s_str("邀请状态中文标签。"),
            "expires_at": s_str("邀请过期时间。", format="date-time"),
            "resend_count": s_int("重发次数。"),
        }, desc="AUTH-17：该员工在本物业最近一条员工邀请；不含 token 摘要，无则为 null。")),
        "active_roles": s_arr(s_str(), "当前生效角色（组名）。"),
        "assignments": s_arr(s_obj({
            "id": s_int(), "role": s_str("角色组名。"), "active": s_bool(),
            "starts_at": s_str("", format="date-time"),
            "ends_at": s_nullable(s_str("", format="date-time")),
        }), "全部角色授权记录。"),
        "source_system": s_str(),
    }),
    "MembershipDto": s_obj({
        "id": s_int(), "member_no": s_str(),
        "guest": s_obj({"id": s_int(), "full_name": s_str()}),
        "level_code": s_str(), "level_label": s_str(),
        "status": s_str(), "status_label": s_str(),
        "joined_at": s_nullable(s_str()), "expires_at": s_nullable(s_str()),
        "legacy_vip_id": s_nullable(s_int("旧 Vip 表映射 ID。")),
        "source_system": s_str(),
    }),
    "MembershipDetailDto": s_obj({
        "level_history": s_arr(s_obj({
            "from_level": s_str(), "to_level": s_str(),
            "reason": s_str(), "changed_at": s_str("", format="date-time"),
        }), "等级变更历史。"),
    }, desc="在 MembershipDto 基础上附加 level_history。"),
}

# ---------------------------------------------------------------------------
# 错误码集合
# ---------------------------------------------------------------------------
E_STAFF = ("400", "401", "403", "404", "500", "503")
E_STAFF_CONFLICT = E_STAFF + ("409",)

ERROR_DESCRIPTIONS = {
    "400": "请求参数或 JSON 请求体校验失败（VALIDATION_ERROR）",
    "401": "未登录或认证凭据无效（AUTHENTICATION_REQUIRED）",
    "403": "当前员工/岗位无权执行此操作（PERMISSION_DENIED）",
    "404": "资源不存在或不属于当前物业（RESOURCE_NOT_FOUND）",
    "409": "业务状态冲突、库存冲突或幂等冲突（BUSINESS_CONFLICT）",
    "429": "请求频率超限；请遵循 Retry-After（RATE_LIMITED，仅公开面）",
    "500": "服务器内部错误（INTERNAL_ERROR）",
    "503": "物业上下文不可用（PROPERTY_UNAVAILABLE / RESOURCE_NOT_FOUND）",
}


def _error_response(code, public=False):
    if code == "429":
        schema = _ref("PublicRateLimitError")
    elif public:
        schema = _ref("PublicApiError")
    else:
        schema = _ref("ApiErrorEnvelope")
    return {"description": ERROR_DESCRIPTIONS[code],
            "content": {"application/json": {"schema": schema}}}


# ---------------------------------------------------------------------------
# 契约描述表（以视图源码为准，逐条核实）
# ---------------------------------------------------------------------------
CONTRACTS = []


def op(path, method, tag, summary, *, status, auth=True, permission=None,
       path_params=(), query=(), body=None, body_required=True, no_body=False,
       ok=(("200", "请求成功"),), data=None, envelope="api", errors=None,
       notes="", extras=None):
    entry = {
        "path": path, "method": method, "tag": tag, "summary": summary,
        "status": status, "auth": auth, "permission": permission,
        "path_params": path_params, "query": query, "body": body,
        "body_required": body_required, "no_body": no_body,
        "ok": ok, "data": data, "envelope": envelope,
        "errors": errors if errors is not None else (E_STAFF if auth else ()),
        "notes": notes, "extras": extras or {},
    }
    CONTRACTS.append(entry)
    return entry


# ========== 公开预订（匿名；凭 Booking Access Token） ==========
RL_SOURCE = "apps.core.rate_limit.DEFAULT_LIMITS（方案 §6.3）"

op("/availability", "get", "公开预订", "查询房型库存与价格",
   status="verified", auth=False,
   query=[Q("check_in", "入住日期 YYYY-MM-DD；不得早于今天。", "string", True, "2026-11-05", fmt="date"),
          Q("check_out", "离店日期 YYYY-MM-DD；必须晚于入住日期。", "string", True, "2026-11-07", fmt="date"),
          Q("rooms", "房间数，1-10，默认 1。", "integer", False, 1),
          Q("room_type_id", "可选；只查询指定房型。", "integer", False, 1)],
   ok=(("200", "物业、日期与逐房型可售结果列表"),),
   data=s_obj({
       "property": s_str("物业代码。"),
       "check_in": s_str("", format="date"), "check_out": s_str("", format="date"),
       "rooms": s_int(),
       "data": s_arr(_ref("RoomTypeAvailability"), "逐房型可售明细（含不可售房型 0 间结果）。"),
   }, desc="raw 响应（不走 {data, meta} 封套）。"),
   envelope="raw", errors=("400", "503"),
   notes="公开匿名查询；单次最长 30 晚。不接受历史日期查询。")

op("/reservations", "post", "公开预订", "创建客房预订",
   status="verified", auth=False,
   body=s_obj({
       "full_name": s_str("住客姓名。", maxLength=80),
       "phone": s_str("住客手机号；参与 phone_hash 限流维度。", maxLength=24),
       "room_type_id": s_int("房型 ID。"),
       "rate_plan_id": s_nullable(s_int("可选；不传由服务按有价计划自动选择。")),
       "check_in": s_str("入住日期 YYYY-MM-DD；不得早于今天。", format="date"),
       "check_out": s_str("离店日期 YYYY-MM-DD；必须晚于入住。", format="date"),
       "rooms": s_int("房间数 1-10，默认 1。", minimum=1, maximum=10, default=1),
       "adults": s_int("成人数 1-10，默认 1。", minimum=1, maximum=10, default=1),
       "children": s_int("儿童数 0-10，默认 0。", minimum=0, maximum=10, default=0),
   }, required=["full_name", "phone", "room_type_id", "check_in", "check_out"]),
   ok=(("201", "预订创建成功，一次性返回 access_token"),
       ("200", "相同 Idempotency-Key 与请求体的幂等重放（返回首次响应）")),
   data=_ref("BookingCreatedResult"),
   envelope="raw", errors=("400", "404", "409", "429", "503"),
   notes="支持 Idempotency-Key 幂等：同 key 同 body 重放返回首次响应（200）；"
         "同 key 不同 body 返回 409。明文 access_token 仅在首次创建响应中返回一次。"
         "限流（§6.3，fail-closed）：IP 10 次/10 分钟 + phone_hash 3 次/10 分钟。"
         "审计事件：public.reservation_blocked / public.reservation_created / public.rate_limited。",
   extras={
       "x-idempotency": {
           "header": "Idempotency-Key", "required": True, "max_length": 128,
           "semantics": "同 key 同请求体 → 重放首次响应(200)；同 key 不同请求体 → 409；记录 TTL 24h。",
           "source": "apps.reservations.services.create_reservation",
       },
       "x-rate-limit": {
           "action": "public.reservation_create", "fail_closed": True,
           "rules": [
               {"dimensions": ["ip"], "limit": 10, "window_seconds": 600},
               {"dimensions": ["phone"], "limit": 3, "window_seconds": 600},
           ],
           "dimension_notes": "ip=REMOTE_ADDR；phone=手机号 HMAC-SHA256 服务端指纹（不存明文）。",
           "source": RL_SOURCE,
       },
       "x-audit-events": ["public.reservation_blocked", "public.reservation_created", "public.rate_limited"],
   })

op("/reservations/{booking_no}", "get", "公开预订", "使用 Booking Access Token 查询预订",
   status="verified", auth=False,
   path_params=[("booking_no", "预订号。")],
   query=[Q("token", "Booking Access Token（也可经 X-Booking-Token 头传递）。", "string", False)],
   ok=(("200", "脱敏预订详情"),), data=_ref("BookingAccessTokenDetail"),
   envelope="raw", errors=("404", "429"),
   notes="必须提供首次创建预订时返回的 Booking Access Token（header X-Booking-Token 或 query token）；"
         "不再使用手机号作为查询凭据。字段按 §6.5 白名单脱敏：姓名掩码，"
         "不返回完整手机号/证件号/provider_ref/内部流水。"
         "限流（§6.3）：IP 30 次/5 分钟 + booking_no+IP 10 次/5 分钟。"
         "审计事件：guest.booking_access_viewed / guest.booking_access_denied / public.rate_limited。",
   extras={
       "x-booking-access-token": "凭据为创建响应一次性返回的 access_token；"
                                 "凭据错误一律 404 并记 guest.booking_access_denied 审计。",
       "x-rate-limit": {
           "action": "public.reservation_query", "fail_closed": True,
           "rules": [
               {"dimensions": ["ip"], "limit": 30, "window_seconds": 300},
               {"dimensions": ["booking", "ip"], "limit": 10, "window_seconds": 300},
           ],
           "dimension_notes": "ip=REMOTE_ADDR；booking=booking_no 规范化大写。",
           "source": RL_SOURCE,
       },
       "x-audit-events": ["guest.booking_access_viewed", "guest.booking_access_denied", "public.rate_limited"],
   })
CONTRACTS[-1]["extras"]["x-booking_token_header_param"] = P(
    "X-Booking-Token", "Booking Access Token（与 query 参数 token 二选一）。", "string", "header", False)

op("/reservations/{booking_no}/cancel", "post", "公开预订", "取消待支付预订",
   status="verified", auth=False,
   path_params=[("booking_no", "预订号。")],
   body=s_obj({"token": s_str("Booking Access Token。")},
              required=["token"]),
   ok=(("200", "取消结果；已取消订单重复取消幂等返回 changed=false"),),
   data=_ref("BookingCancelResult"),
   envelope="raw", errors=("400", "404", "409", "429"),
   notes="凭 Booking Access Token 取消；仅待支付订单可取消。"
         "限流（§6.3）：IP 10 次/10 分钟 + booking_no+IP 5 次/10 分钟。"
         "审计事件：guest.booking_cancelled / guest.booking_access_denied / public.rate_limited。",
   extras={
       "x-booking-access-token": "请求体 token 字段；凭据错误一律 404 并记审计。",
       "x-rate-limit": {
           "action": "public.reservation_cancel", "fail_closed": True,
           "rules": [
               {"dimensions": ["ip"], "limit": 10, "window_seconds": 600},
               {"dimensions": ["booking", "ip"], "limit": 5, "window_seconds": 600},
           ],
           "source": RL_SOURCE,
       },
       "x-audit-events": ["guest.booking_cancelled", "guest.booking_access_denied", "public.rate_limited"],
       "x-idempotency": {"semantics": "已取消订单重复取消直接返回当前状态（changed=false），不再变更。"},
   })

op("/payments/intents", "post", "支付", "创建本地模拟支付意图",
   status="verified", auth=False,
   body=s_obj({
       "booking_no": s_str("预订号。"),
       "token": s_str("Booking Access Token。"),
   }, required=["booking_no", "token"]),
   ok=(("201", "模拟支付单创建成功"),),
   data=_ref("PaymentIntentResult"),
   envelope="raw", errors=("400", "403", "404", "409", "429"),
   notes="仅 dev/test 环境开放；生产环境一律 403。凭 Booking Access Token，不使用手机号。"
         "限流（§6.3）：IP 10 次/5 分钟 + booking_no 5 次/5 分钟。"
         "审计事件：guest.booking_access_denied / public.rate_limited。",
   extras={
       "x-booking-access-token": "请求体 token 字段；凭据错误一律 404 并记审计。",
       "x-rate-limit": {
           "action": "public.payment_intent", "fail_closed": True,
           "rules": [
               {"dimensions": ["ip"], "limit": 10, "window_seconds": 300},
               {"dimensions": ["booking"], "limit": 5, "window_seconds": 300},
           ],
           "source": RL_SOURCE,
       },
       "x-audit-events": ["guest.booking_access_denied", "public.rate_limited"],
       "x-environment-gate": "AUTH-07：settings.DEBUG 且 DJANGO_ENV in {dev,test} 才允许；否则 403。",
   })

op("/payments/callback", "post", "支付", "处理本地模拟支付回调",
   status="verified", auth=False,
   body=s_obj({
       "provider_ref": s_str("模拟支付流水号。"),
       "amount": s_str("金额（字符串十进制）。"),
       "status": s_str("SUCCESS / FAILED。"),
   }, required=["provider_ref", "amount", "status"]),
   ok=(("200", "回调处理结果"),), data=_ref("MockCallbackResult"),
   envelope="raw", errors=("400", "401", "404", "409"),
   notes="仅本地 dev/test 环境可用（其他环境 404）；需要 X-Signature HMAC 签名（失败 401）；"
         "视图 csrf_exempt，无需 CSRF 令牌。",
   extras={
       "x-signature": {
           "header": "X-Signature", "required": True,
           "algorithm": "sign_mock_callback HMAC（apps/billing/services.py）",
       },
       "x-environment-gate": "仅 settings.DEBUG 且 DJANGO_ENV in {dev,test}；否则 404。",
   })

# ========== 前台接待（Session + 员工任职门禁） ==========
FD_READ_PERM = "有效员工任职（DEFAULT 物业）+ hotelapp.view_accommodation"
FD_WRITE_PERM = "有效员工任职（DEFAULT 物业）+ hotelapp.view_accommodation + hotelapp.change_accommodation"

fd_read = [
    ("/front-desk/overview", "前台总览", "verified",
     [Q("date", "业务日期 YYYY-MM-DD；缺省取物业当前营业日。", "string", False, "2026-11-05")],
     "arrivals_count/unassigned_count/departures_count/in_house_count/clean_available_count/"
     "waiting_release_count/maintenance_count 与 exception_counts"),
    ("/front-desk/arrivals", "抵店列表", "verified",
     [Q("date", "抵店日期；缺省取物业当前营业日。", "string", False),
      Q("q", "搜索词（预订号/姓名/手机号/房号，≤80 字符）。"),
      Q("assigned", "排房筛选：all/yes/no。"),
      *PQ()], "ReservationRoomSummary 数组（当天待抵店明细）"),
    ("/front-desk/departures", "离店列表", "partial",
     [Q("date", "离店日期；缺省取物业当前营业日。", "string", False),
      Q("q", "搜索词。"),
      *PQ()], "ReservationRoomSummary 数组（当天在住待离店）"),
    ("/front-desk/in-house", "在住列表", "partial",
     [Q("q", "搜索词。"), Q("room_type_id", "房型 ID。", "integer"),
      Q("floor", "楼层。"), *PQ()], "ReservationRoomSummary 数组（全部在住 Stay）"),
    ("/front-desk/reservations", "预订列表", "partial",
     [Q("q", "搜索词（预订号/姓名/手机号）。"),
      Q("status", "预订状态（PENDING/CONFIRMED/CANCELLED/EXPIRED/CHECKED_IN/CHECKED_OUT/NO_SHOW）。"),
      Q("source", "来源（WEBSITE/WALK_IN/OTA/PHONE）。"),
      *PQ()], "FrontDeskReservationListItem 数组"),
    ("/front-desk/rooms", "房间列表", "verified",
     [Q("floor", "楼层。"),
      Q("readiness", "READY/INSPECTING/MAINTENANCE/ALL（缺省 ALL）。")],
     "FrontDeskRoomRow 数组"),
    ("/front-desk/rooms/assignable", "可分配房间", "partial",
     [Q("reservation_room_id", "预订房间明细 ID（必填）。", "integer", True)],
     "AssignableRoom 数组；同型洁净空闲且不与在途预订重叠"),
    ("/front-desk/legacy-reservations", "历史预订列表", "verified",
     [Q("q", "搜索词（旧姓名/联系电话/旧订单 ID）。"),
      Q("status", "旧系统状态原文筛选。"),
      *PQ()], "LegacyReservationDto 数组（只读历史档案）"),
]
for path, summary, status, query, data_desc in fd_read:
    op(path, "get", "前台接待", summary, status=status,
       path_params=([("booking_no", "预订号。")] if "{booking_no}" in path else
                    [("legacy_id", "旧 hotelapp 订单 ID。")] if "{legacy_id}" in path else
                    [("stay_id", "在住 Stay ID。")] if "{stay_id}" in path else []),
       query=query, permission=FD_READ_PERM,
       ok=(("200", "查询结果"),),
       data=(s_arr(_ref("LegacyReservationDto"), data_desc) if "legacy" in path and "detail" not in summary
             else _ref("LegacyReservationDto") if "{legacy_id}" in path
             else s_arr(_ref("ReservationRoomSummary"), data_desc) if summary in ("抵店列表", "离店列表", "在住列表")
             else s_arr(_ref("FrontDeskReservationListItem"), data_desc) if summary == "预订列表"
             else s_arr(_ref("FrontDeskRoomRow"), data_desc) if summary == "房间列表"
             else s_arr(_ref("AssignableRoom"), data_desc) if summary == "可分配房间"
             else _ref("FrontDeskReservationDetail")),
       envelope="fd", errors=E_STAFF)

op("/front-desk/reservations/{booking_no}", "get", "前台接待", "预订详情",
   status="partial", path_params=[("booking_no", "预订号。")],
   permission=FD_READ_PERM, ok=(("200", "预订主单 + 房间明细 + Stay + 账单摘要 + 可执行操作"),),
   data=_ref("FrontDeskReservationDetail"), envelope="fd", errors=E_STAFF)

op("/front-desk/folios/{booking_no}", "get", "前台接待", "预订账单详情",
   status="verified", path_params=[("booking_no", "预订号。")],
   permission=FD_READ_PERM, ok=(("200", "账单 summary + entries + payments + 允许操作"),),
   data=_ref("FolioDetailData"), envelope="fd", errors=E_STAFF)

op("/front-desk/legacy-reservations/{legacy_id}", "get", "前台接待", "历史预订详情",
   status="partial", path_params=[("legacy_id", "旧 hotelapp 订单 ID。")],
   permission=FD_READ_PERM, ok=(("200", "历史订单 + 旧 Folio 凭证原样展示"),),
   data=s_obj({
       **{k: v for k, v in COMPONENTS["LegacyReservationDto"]["properties"].items()},
       "legacy_entries": s_arr(_ref("LegacyFolioEntryDto"), "旧账凭证（原样展示，不与 PMS2 对账）。"),
       "legacy_note": s_str("口径说明。"),
       "timeline": s_obj(desc="旧单无时间线，恒为空结构。"),
   }, desc="LegacyReservationDto + legacy_entries。"),
   envelope="fd", errors=E_STAFF)

op("/front-desk/stays/{stay_id}/available-rooms", "get", "前台接待", "在住订单可换房列表",
   status="partial", path_params=[("stay_id", "在住 Stay ID。")],
   permission=FD_READ_PERM, ok=(("200", "同房型洁净空闲且不冲突的房间列表"),),
   data=s_arr(s_obj({"id": s_int(), "number": s_str(), "floor": s_str()})),
   envelope="fd", errors=E_STAFF)

fd_write = [
    ("/front-desk/reservation-rooms/{reservation_room_id}/assign", "分配房间", "verified",
     [("reservation_room_id", "预订房间明细 ID。")],
     s_obj({"room_id": s_int("目标房间 ID（须可分配）。")}, required=["room_id"]),
     s_obj({"reservation_room_id": s_int(),
            "assigned_room": s_obj({"id": s_int(), "number": s_str(), "floor": s_str()})}),
     "changed 标志在 data 外层；新分配 201，改派 200。", FD_WRITE_PERM,
     (("201", "房间已分配"), ("200", "改派成功（已有房间的明细）"))),
    ("/front-desk/reservation-rooms/{reservation_room_id}/check-in", "办理入住", "verified",
     [("reservation_room_id", "预订房间明细 ID。")], None,
     s_obj({"stay_id": s_int(), "reservation_room_id": s_int(), "room_id": s_int(),
            "room_number": s_str(), "status": s_str(),
            "actual_check_in": s_nullable(s_str("", format="date-time"))}),
     "请求体可为空 JSON；创建 Stay 201，重复入住幂等 200。", FD_WRITE_PERM,
     (("201", "入住成功创建 Stay"), ("200", "已有在住 Stay（幂等）"))),
    ("/front-desk/reservations/{booking_no}/payments", "收取预订款项", "verified",
     [("booking_no", "预订号。")],
     s_obj({"amount": s_str("收款金额（大于零）。"), "method": s_str("CASH/CARD/TRANSFER。")},
           required=["amount", "method"]),
     s_obj({"payment_id": s_int(), "provider_ref": s_nullable(s_str()),
            "method": s_str(), "amount": s_str(), "currency": s_str(), "status": s_str()}),
     "Idempotency-Key 必填（1-64 字符，服务强校验）；BillingService 统一入账。", FD_WRITE_PERM,
     (("201", "收款成功"), ("200", "相同幂等键重放"))),
    ("/front-desk/stays/{stay_id}/check-out", "办理退房", "verified",
     [("stay_id", "在住 Stay ID。")], None,
     s_obj({"stay_id": s_int(), "status": s_str(),
            "actual_check_out": s_nullable(s_str("", format="date-time")),
            "room_id": s_int(), "room_number": s_str()}),
     "请求体可为空 JSON。", FD_WRITE_PERM,
     (("200", "退房完成"),)),
    ("/front-desk/stays/{stay_id}/extend", "延长入住", "verified",
     [("stay_id", "在住 Stay ID。")],
     s_obj({"new_check_out": s_str("新的离店日期（必须晚于当前）。", format="date"),
            "reason": s_nullable(s_str("延长原因。"))}, required=["new_check_out"]),
     s_obj({"stay_id": s_int(), "check_out": s_str("", format="date"),
            "extension_amount": s_str("延夜新增房费。"),
            "new_room_amount": s_str("明细总房费。")}),
     "按每日房价快照计算延夜差额。", FD_WRITE_PERM,
     (("200", "延住成功；返回差额与新房费总额"),)),
    ("/front-desk/stays/{stay_id}/change-room", "在住换房", "verified",
     [("stay_id", "在住 Stay ID。")],
     s_obj({"room_id": s_int("新房间 ID。"), "reason": s_nullable(s_str("换房原因。"))},
           required=["room_id"]),
     s_obj({"stay_id": s_int(), "room_id": s_int(), "room_number": s_str(),
            "room_segments": s_arr(s_obj({
                "room_number": s_str(), "started_at": s_str("", format="date-time"),
                "ended_at": s_nullable(s_str("", format="date-time")),
                "reason": s_nullable(s_str()),
            }), "换房段记录。")}),
     "写 StayRoomSegment 换房轨迹。", FD_WRITE_PERM,
     (("200", "换房成功；changed 标志在 data 外层"),)),
    ("/front-desk/reservation-rooms/{reservation_room_id}/no-show", "标记 No-show", "verified",
     [("reservation_room_id", "预订房间明细 ID。")],
     s_obj({"reason": s_str("No-show 原因，1-240 字符（服务强校验）。")}, required=["reason"]),
     s_obj({"no_show_id": s_int(), "reservation_room_id": s_int(),
            "released_nights": s_int("释放库存晚数。"), "status": s_str("NO_SHOW")}),
     "独立权限 hotelapp.no_show_handle（不随入住修改权继承）；首记 201、重复幂等 200。",
     "有效员工任职（DEFAULT 物业）+ hotelapp.no_show_handle",
     (("201", "No-show 记录创建"), ("200", "已有记录（幂等）"))),
    ("/front-desk/reservations/{booking_no}/deposits", "收取押金", "verified",
     [("booking_no", "预订号。")],
     s_obj({"amount": s_str("押金金额（大于零）。"), "method": s_str("CASH/CARD/TRANSFER。")},
           required=["amount", "method"]),
     s_obj({"payment_id": s_int(), "provider_ref": s_nullable(s_str()),
            "amount": s_str(), "method": s_str(), "status": s_str(),
            "payment_kind": s_str("固定 DEPOSIT。")}),
     "Idempotency-Key 必填（1-64 字符）；仅已确认/在住预订可收押金。", FD_WRITE_PERM,
     (("201", "押金登记成功"), ("200", "相同幂等键重放"))),
    ("/front-desk/payments/{payment_id}/refunds", "申请退款", "verified",
     [("payment_id", "原支付单 ID。")],
     s_obj({"amount": s_str("退款金额（大于零）。"),
            "reason": s_str("退款原因，1-240 字符（服务强校验）。")}, required=["amount", "reason"]),
     s_obj({"refund_id": s_int(), "provider_ref": s_nullable(s_str()),
            "payment_id": s_int(), "amount": s_str(), "status": s_str("REFUND_PENDING")}),
     "Idempotency-Key 必填（1-64 字符）；生成双人审批记录（申请人≠审批人）。",
     "有效员工任职（DEFAULT 物业）+ billing.refund_request",
     (("201", "退款申请已提交待审批"), ("200", "相同幂等键重放"))),
    ("/front-desk/refunds/{refund_id}/approve", "审批退款", "verified",
     [("refund_id", "退款申请 ID。")],
     s_obj({"confirm_refund_paid": s_bool("必须显式传 true：确认人工退款已实际完成。")},
           required=["confirm_refund_paid"]),
     s_obj({"refund_id": s_int(), "status": s_str("REFUNDED"),
            "approved_by": s_nullable(s_int("审批人 ID。"))}),
     "独立权限 billing.refund_approve；confirm_refund_paid 非 true 直接 400。",
     "有效员工任职（DEFAULT 物业）+ billing.refund_approve",
     (("200", "退款审批入账完成"),)),
]
for path, summary, status, pp, body, data, note, perm, ok in fd_write:
    op(path, "post", "前台接待", summary, status=status, path_params=pp, body=body,
       body_required=bool(body), no_body=body is None, permission=perm,
       ok=ok, data=data, envelope="fd", errors=E_STAFF_CONFLICT, notes=note)

# 服务层强校验 Idempotency-Key（1-64 字符必填）的三个收款/押金/退款端点。
for _path in ("/front-desk/reservations/{booking_no}/payments",
              "/front-desk/reservations/{booking_no}/deposits",
              "/front-desk/payments/{payment_id}/refunds"):
    _entry = next(e for e in CONTRACTS if e["path"] == _path)
    _entry["extras"]["x-idempotency"] = {
        "header": "Idempotency-Key", "required": True, "max_length": 64,
        "semantics": "服务强校验必填；同 key 同 body 重放首次响应（服务侧幂等）。",
        "source": "apps.reservations.front_desk_services / lifecycle_services / BillingService",
    }

# ========== 夜审与紧急访问（Session + 专项权限） ==========
op("/night-audit/preview", "get", "夜审与紧急访问", "预览夜审",
   status="verified",
   query=[Q("date", "营业日期 YYYY-MM-DD；缺省取物业当前营业日。", "string", False)],
   permission="有效员工 + core.night_audit_view",
   ok=(("200", "夜审预览（待过账房费行 + 汇总）"),),
   data=_ref("NightAuditPreviewResult"), errors=E_STAFF)

op("/night-audit/run", "post", "夜审与紧急访问", "执行夜审",
   status="verified", body_required=False,
   body=s_obj({"date": s_nullable(s_str("营业日期；缺省取当前营业日。", format="date"))},
              desc="可为空 JSON 对象。"),
   permission="有效员工 + hotelapp.night_audit_execute",
   ok=(("201", "夜审执行完成"), ("200", "该营业日已夜审（返回已有结果）")),
   data=_ref("NightAuditRunResult"), errors=E_STAFF_CONFLICT,
   notes="串行化同一物业并发夜审；生成营业日关账与房费过账。")

op("/night-audit/business-dates/{business_date_id}/reopen-request", "post", "夜审与紧急访问", "申请重开营业日",
   status="verified", path_params=[("business_date_id", "营业日记录 ID。")],
   body=s_obj({"reason": s_str("重开原因，1-240 字符（服务强校验）。")}, required=["reason"]),
   permission="有效员工 + hotelapp.business_date_reopen",
   ok=(("201", "重开申请已创建（生成审批）"), ("200", "已有待审申请")),
   data=_ref("ReopenRequestResult"), errors=E_STAFF_CONFLICT)

op("/night-audit/business-dates/{business_date_id}/approvals/{approval_id}/approve", "post",
   "夜审与紧急访问", "审批重开营业日",
   status="verified",
   path_params=[("business_date_id", "营业日记录 ID。"), ("approval_id", "重开审批记录 ID。")],
   no_body=True, permission="有效员工 + hotelapp.business_date_reopen",
   ok=(("200", "审批通过并重开营业日"),),
   data=_ref("ReopenApproveResult"), errors=E_STAFF_CONFLICT)

op("/night-audit/business-dates/{business_date_id}/break-glass", "post", "夜审与紧急访问", "申请紧急访问",
   status="verified", path_params=[("business_date_id", "营业日记录 ID。")],
   body=s_obj({
       "password": s_str("登录账号密码（二次验证）。"),
       "reason": s_str("紧急原因，8-240 字符（服务强校验）。"),
   }, required=["password", "reason"]),
   permission="有效员工 + hotelapp.business_date_reopen（仅单一有效经理账号可申请）",
   ok=(("201", "十分钟紧急授权已发放"),),
   data=_ref("BreakGlassRequestResult"), errors=E_STAFF_CONFLICT)

op("/night-audit/business-dates/{business_date_id}/break-glass/{record_id}/execute", "post",
   "夜审与紧急访问", "执行紧急访问",
   status="verified",
   path_params=[("business_date_id", "营业日记录 ID。"), ("record_id", "紧急授权记录 ID。")],
   no_body=True,
   permission="有效员工 + hotelapp.business_date_reopen（须原申请账号在 10 分钟内执行）",
   ok=(("200", "营业日已重开；须另一账号事后复核"),),
   data=_ref("BreakGlassExecuteResult"), errors=E_STAFF_CONFLICT)

op("/night-audit/break-glass/{record_id}/review", "post", "夜审与紧急访问", "复核紧急访问记录",
   status="verified", path_params=[("record_id", "紧急授权记录 ID。")],
   body=s_obj({
       "decision": s_str("CONFIRMED / REJECTED。"),
       "reason": s_str("复核说明，8-240 字符（服务强校验）。"),
   }, required=["decision", "reason"]),
   permission="有效员工 + core.breakglass_review（复核账号必须不同于紧急操作账号）",
   ok=(("200", "复核结论已记录"),),
   data=_ref("BreakGlassReviewResult"), errors=E_STAFF_CONFLICT)

# ---- §4.2 营业日中心只读端点（读取口径：hotelapp.night_audit_execute） ----
op("/night-audit/business-dates", "get", "夜审与紧急访问", "营业日分页列表",
   status="verified",
   query=[Q("status", "可选；OPEN/CLOSED/REOPENED，非法值 400。", "string", False, "CLOSED"),
          *PQ()],
   permission="有效员工 + core.night_audit_view",
   ok=(("200", "营业日 DTO 数组（含最近一次夜审运行摘要），稳定排序 -business_date,-id"),),
   data=s_arr(_ref("BusinessDateDto")), errors=E_STAFF)

op("/night-audit/business-dates/current", "get", "夜审与紧急访问", "当前营业日",
   status="verified",
   permission="有效员工 + core.night_audit_view",
   ok=(("200", "当前营业日（按物业时区解析；无记录时 exists=false 且按 OPEN 口径返回）"),),
   data=_ref("BusinessDateCurrentResult"), errors=E_STAFF)

op("/night-audit/business-dates/{business_date_id}", "get", "夜审与紧急访问", "营业日详情",
   status="verified", path_params=[("business_date_id", "营业日记录 ID。")],
   permission="有效员工 + core.night_audit_view",
   ok=(("200", "营业日详情：夜审运行历史、重开审批、过租/未到汇总、异常房态"),),
   data=_ref("BusinessDateDetail"), errors=E_STAFF_CONFLICT)

op("/night-audit/break-glass", "get", "夜审与紧急访问", "紧急访问记录分页列表",
   status="verified",
   query=[Q("status", "可选；GRANTED/USED/EXPIRED/REVIEWED。", "string", False, "REVIEWED"),
          Q("review_decision", "可选；CONFIRMED/REJECTED。", "string", False),
          *PQ()],
   permission="有效员工 + core.night_audit_view",
   ok=(("200", "break-glass 记录数组（review_pending 标记待复核），稳定排序 -created_at,-id"),),
   data=s_arr(_ref("BreakGlassRecordDto")), errors=E_STAFF)

op("/night-audit/runs", "get", "夜审与紧急访问", "夜审历史分页列表",
   status="verified",
   query=[Q("status", "可选；RUNNING/SUCCESS/FAILED。", "string", False, "FAILED"),
          Q("business_date_id", "可选；按营业日记录 ID 过滤。", "integer", False, 1),
          *PQ()],
   permission="有效员工 + core.night_audit_view",
   ok=(("200", "夜审运行 DTO 数组（含耗时与截断失败原因），稳定排序 -started_at,-id"),),
   data=s_arr(_ref("NightAuditRunDto")), errors=E_STAFF)

op("/night-audit/runs/{run_id}", "get", "夜审与紧急访问", "单次夜审运行详情",
   status="verified", path_params=[("run_id", "夜审运行记录 ID。")],
   permission="有效员工 + core.night_audit_view",
   ok=(("200", "运行详情（完整摘要 summary 与失败原因）"),),
   data=_ref("NightAuditRunDto"), errors=E_STAFF_CONFLICT)

# ========== 住客中心（Session + hotelapp.view_client 等） ==========
op("/guests", "get", "住客中心", "住客列表",
   status="partial",
   query=[Q("q", "姓名/联系方式搜索。"), *PQ()],
   permission="有效员工 + hotelapp.view_client",
   ok=(("200", "Guest DTO 数组（按权限脱敏）"),),
   data=s_arr(_ref("GuestDto")), errors=E_STAFF)

op("/guests/{guest_id}", "get", "住客中心", "住客详情",
   status="verified", path_params=[("guest_id", "住客档案 ID。")],
   permission="有效员工 + hotelapp.view_client",
   ok=(("200", "Guest 360：档案 + 证件 + 电话 + 会员 + 预订/Stay/Folio/旧映射摘要"),),
   data=s_obj({**COMPONENTS["GuestDto"]["properties"], **COMPONENTS["GuestDetailDto"]["properties"]},
              desc="GuestDto + documents/phones/notes/reservations/stays/folios"
                   "/legacy_links（基线 2.0 §3.2：Folio 为账本口径余额摘要；"
                   "legacy_links 为旧 Client/Vip → Guest 映射核验状态）。"),
   errors=E_STAFF)

op("/guests/{guest_id}/timeline", "get", "住客中心", "住客 360 操作时间线",
   status="verified", path_params=[("guest_id", "住客档案 ID。")],
   query=[Q("limit", "返回条数上限，10-200，默认 100。", "integer", False, 100)],
   permission="有效员工 + hotelapp.view_client",
   ok=(("200", "预订/入住/账务/支付退款/会员/审计六类事件按时间倒序合并"),),
   data=s_obj({
       "guest_id": s_int(),
       "count": s_int("本次返回条数（≤ limit）。"),
       "items": s_arr(s_obj({
           "time": s_str("ISO 8601 时间。", format="date-time"),
           "kind": s_str("reservation / stay.check_in / stay.check_out / "
                         "ledger_entry / payment / refund / "
                         "membership.level_change / audit。"),
           "summary": s_obj(desc="按 kind 区分的事件摘要；审计事件只含动作/实体/"
                                 "原因/操作人，不含 before/after 原文。"),
       }), desc="时间倒序合并（同一时刻按业务事实→审计顺序稳定输出）。"),
   }),
   errors=E_STAFF,
   notes="基线 2.0 §3.2 只读聚合端点：全部实时查询，不落第二份汇总数据。")

op("/guests/{guest_id}/folios", "get", "住客中心", "住客账单摘要",
   status="verified", path_params=[("guest_id", "住客档案 ID。")],
   query=[*PQ()],
   permission="有效员工 + hotelapp.view_client",
   ok=(("200", "Folio 头 + 账本口径余额（分页）；不展开逐条凭证"),),
   data=s_arr(s_obj({
       "id": s_int(), "folio_type": s_str(), "folio_type_label": s_str(),
       "status": s_str(), "status_label": s_str(),
       "currency": s_str(), "balance": s_str("带符号 LedgerEntry 汇总，Decimal→str。"),
       "booking_no": s_nullable(s_str()),
       "opened_at": s_str("", format="date-time"),
       "closed_at": s_nullable(s_str("", format="date-time")),
       "closed_by": s_nullable(s_str()),
       "source_system": s_str(),
   }), desc="账单摘要行数组"),
   errors=E_STAFF,
   notes="基线 2.0 §3.2 接口表：账单摘要按角色脱敏——逐条凭证/支付明细需"
         "hotelapp.finance_access 访问 /api/v1/billing/folios/{id}。")

op("/guests/create", "post", "住客中心", "创建住客档案",
   status="partial",
   body=s_obj({
       "full_name": s_str("客人姓名（服务强校验非空）。"),
       "phone": s_nullable(s_str("手机号；共享电话仅标记人工核验，不自动合并档案。")),
       "email": s_nullable(s_str("电子邮件。")), "notes": s_nullable(s_str("备注。")),
   }, required=["full_name"]),
   permission="有效员工 + hotelapp.add_client",
   ok=(("201", "客人档案创建成功"),),
   data=s_obj({"id": s_int(), "full_name": s_str()}),
   errors=E_STAFF_CONFLICT,
   extras={"x-idempotency": {"header": "Idempotency-Key", "required": False, "max_length": 128,
                             "semantics": "api_contract.idempotent：同 key 同 body 重放首次响应；不同 body 409；TTL 24h。"}})

op("/guests/{guest_id}/update", "post", "住客中心", "更新住客档案",
   status="verified", path_params=[("guest_id", "住客档案 ID。")],
   body=s_obj({
       "full_name": s_nullable(s_str()), "phone": s_nullable(s_str()),
       "email": s_nullable(s_str()), "notes": s_nullable(s_str()),
   }, desc="所有字段可选；仅传入字段被更新，响应返回实际变更字段列表。"),
   permission="有效员工 + hotelapp.change_client",
   ok=(("200", "更新成功；返回 changed 字段列表"),),
   data=s_obj({"id": s_int(), "changed": s_arr(s_str(), "实际变更的字段名。")}),
   errors=E_STAFF_CONFLICT,
   extras={"x-idempotency": {"header": "Idempotency-Key", "required": False, "max_length": 128,
                             "semantics": "api_contract.idempotent：同 key 同 body 重放首次响应；不同 body 409；TTL 24h。"}})

op("/guests/{guest_id}/documents/{document_id}/reveal", "post", "敏感数据", "揭示住客证件信息",
   status="verified", path_params=[("guest_id", "住客档案 ID。"), ("document_id", "证件 ID。")],
   body=s_obj({"reason": s_str("业务用途/原因，8-240 字符（强校验）。")}, required=["reason"]),
   permission="有效员工 + hotelapp.guest_document_reveal（独立专项权限）",
   ok=(("200", "返回证件完整值；逐次写审计"),),
   data=s_obj({"guest_id": s_int(), "document_id": s_int(),
               "doc_type": s_str(), "document_no": s_str("证件完整值（解密后明文）。")}),
   errors=E_STAFF_CONFLICT,
   notes="按需揭示（break-glass 之外的常规 PII 揭示）：独立权限 + 强制原因 + 逐次审计"
         "（guest.pii_reveal.document）。")

op("/guests/{guest_id}/phones/{phone_id}/reveal", "post", "敏感数据", "揭示住客电话",
   status="verified", path_params=[("guest_id", "住客档案 ID。"), ("phone_id", "电话记录 ID。")],
   body=s_obj({"reason": s_str("业务用途/原因，8-240 字符（强校验）。")}, required=["reason"]),
   permission="有效员工 + hotelapp.guest_phone_reveal（与证件揭示分权）",
   ok=(("200", "返回电话完整值；逐次写审计"),),
   data=s_obj({"guest_id": s_int(), "phone_id": s_int(), "phone": s_str("电话完整值。")}),
   errors=E_STAFF_CONFLICT,
   notes="独立权限 + 强制原因 + 逐次审计（guest.pii_reveal.phone）。")

# ========== 房态库存 / 房价管理 / 财务 ==========
op("/rooms/status", "get", "房态库存", "房态列表",
   status="partial",
   query=[Q("floor", "楼层。"), Q("housekeeping", "清洁状态筛选。"),
          Q("room_type_id", "房型 ID。", "integer"), Q("q", "房号/房型名搜索。"), *PQ()],
   permission="有效员工 + hotelapp.view_room",
   ok=(("200", "三维房态行数组"),),
   data=s_arr(_ref("RoomStatusRow")), errors=E_STAFF)

op("/room-types", "get", "房态库存", "房型列表",
   status="partial", permission="有效员工 + hotelapp.view_room",
   ok=(("200", "当前物业启用房型数组"),),
   data=s_arr(_ref("RoomTypeRow")), errors=E_STAFF)

op("/rate-calendar", "get", "房价管理", "房价日历",
   status="partial",
   query=[Q("start", "开始日期 YYYY-MM-DD；缺省今天。", "string", False, "2026-11-05", fmt="date"),
          Q("days", "查询天数 1-30，默认 14。", "integer", False, 14),
          Q("room_type_id", "房型 ID。", "integer")],
   permission="有效员工 + hotelapp.view_room",
   ok=(("200", "逐日库存 + 逐日房价"),),
   data=_ref("RateCalendarData"), errors=E_STAFF)

op("/financial-summary", "get", "财务", "财务汇总",
   status="partial", permission="有效员工 + hotelapp.finance_access",
   ok=(("200", "近 7 天收支汇总；值班经理仅汇总不见明细"),),
   data=_ref("FinancialSummaryData"), errors=E_STAFF)

op("/daily-rates/{rate_id}/adjust", "post", "房价管理", "调整每日房价",
   status="partial", path_params=[("rate_id", "DailyRate ID。")],
   body=s_obj({"price": s_str("新价格（字符串十进制；服务校验精度）。"),
               "reason": s_str("调价原因，1-240 字符（服务强校验）。")}, required=["price", "reason"]),
   permission="有效员工 + inventory.manage_rates（店长专项权限）",
   ok=(("200", "调价结果；同价不改为 changed=false"),),
   data=_ref("DailyRateAdjustResult"), errors=E_STAFF_CONFLICT,
   extras={"x-idempotency": {"header": "Idempotency-Key", "required": False, "max_length": 128,
                             "semantics": "api_contract.idempotent：同 key 同 body 重放首次响应；不同 body 409；TTL 24h。"}})

# ========== 财务与账单中心（基线 2.0 §4.1；读=hotelapp.finance_access，写走 BillingService） ==========
FIN_READ_PERM = ("有效员工（DEFAULT 有效任职）+ hotelapp.finance_access；"
                 "QuerySet 强制当前物业过滤，金额一律 Decimal→str，账本只读展示")

op("/billing/folios", "get", "财务", "账单列表",
   status="verified",
   query=[Q("status", "账单状态筛选 OPEN/CLOSED/VOIDED。"),
          Q("booking_no", "预订号精确筛选。"),
          Q("folio_type", "账单类型 MASTER/GUEST/COMPANY/HOUSE/DEPOSIT。"),
          Q("q", "预订号/住客姓名包含搜索。"), *PQ()],
   permission=FIN_READ_PERM + "；meta.pagination.summary 汇总未结账单数与账本口径余额",
   ok=(("200", "当前物业账单数组（含逐单余额）"),),
   data=s_arr(s_obj({
       "id": s_int(), "folio_type": s_str(), "folio_type_label": s_str(),
       "status": s_str(), "status_label": s_str(),
       "currency": s_str(), "currency_label": s_str(),
       "balance": s_str("账本口径余额（带符号 LedgerEntry 汇总，Decimal→str）。"),
       "reservation_id": s_int(), "booking_no": s_str(),
       "reservation_status": s_str(), "guest_id": s_int(), "guest_name": s_str(),
       "opened_at": s_str("", format="date-time"),
       "closed_at": s_nullable(s_str("", format="date-time")),
   }), desc="账单行数组"), errors=E_STAFF)

op("/billing/folios/{folio_id}", "get", "财务", "账单详情（流水/支付/退款明细）",
   status="verified", path_params=[("folio_id", "Folio ID。")],
   permission=FIN_READ_PERM + "；非本物业账单一律 404",
   ok=(("200", "账单头 + summary + entries + payments（嵌套 refunds）"),),
   data=s_obj({
       "id": s_int(), "folio_type": s_str(), "folio_type_label": s_str(),
       "status": s_str(), "status_label": s_str(), "currency": s_str(),
       "currency_label": s_str(),
       "opened_at": s_str("", format="date-time"),
       "closed_at": s_nullable(s_str("", format="date-time")),
       "reservation": s_obj({"id": s_int(), "booking_no": s_str(),
                             "status": s_str(), "status_label": s_str()}),
       "guest": s_obj({"id": s_int(), "full_name": s_str()}),
       "summary": s_obj({"charges": s_str(), "credits": s_str(), "balance": s_str()}),
       "entries": s_arr(s_obj({
           "id": s_int(), "entry_type": s_str(), "entry_type_label": s_str(),
           "amount": s_str(), "currency": s_str(),
           "business_date": s_str("", format="date"),
           "reference": s_str(), "description": s_str(),
           "void_of_id": s_nullable(s_int()), "void_reason": s_str(),
           "posted_by": s_nullable(s_str()),
           "created_at": s_str("", format="date-time"),
       }), desc="不可变凭证行（只追加事实展示）"),
       "payments": s_arr(s_obj({
           "id": s_int(), "provider": s_str(), "provider_ref": s_str(),
           "method": s_str(), "amount": s_str(), "currency": s_str(),
           "status": s_str(), "status_label": s_str(),
           "paid_at": s_nullable(s_str("", format="date-time")),
           "created_at": s_str("", format="date-time"),
           "refunds": s_arr(s_obj({
               "id": s_int(), "amount": s_str(), "reason": s_str(),
               "status": s_str(), "status_label": s_str(),
               "provider_ref": s_str(),
               "requested_by": s_nullable(s_str()),
               "approved_by": s_nullable(s_str()),
               "created_at": s_str("", format="date-time"),
               "processed_at": s_nullable(s_str("", format="date-time")),
           })),
       })),
   }), errors=E_STAFF)

op("/billing/ledger", "get", "财务", "不可变流水列表",
   status="verified",
   query=[Q("entry_type", "凭证类型 ROOM_CHARGE/DEPOSIT/PAYMENT/REFUND/ADJUSTMENT/REVERSAL。"),
          Q("from", "营业日起 YYYY-MM-DD。", "string", False, "2026-11-01", fmt="date"),
          Q("to", "营业日止 YYYY-MM-DD。", "string", False, "2026-11-06", fmt="date"),
          Q("folio_id", "按账单筛选。", "integer"), *PQ()],
   permission=FIN_READ_PERM + "；LedgerEntry 只追加，不允许编辑/删除",
   ok=(("200", "凭证行数组（倒序，含冲销关联 void_of_id）"),),
   data=s_arr(s_obj({
       "id": s_int(), "folio_id": s_int(), "booking_no": s_str(),
       "entry_type": s_str(), "entry_type_label": s_str(),
       "amount": s_str("带符号金额（Decimal→str）。"), "currency": s_str(),
       "business_date": s_str("", format="date"),
       "reference": s_str("全局唯一凭证号。"), "description": s_str(),
       "void_of_id": s_nullable(s_int("红字冲销指向的原凭证。")),
       "void_reason": s_str(), "posted_by": s_nullable(s_str()),
       "created_at": s_str("", format="date-time"),
   })), errors=E_STAFF)

op("/billing/payments", "get", "财务", "支付流水列表",
   status="verified",
   query=[Q("status", "支付状态筛选（CREATED/PENDING/SUCCEEDED/...）。"),
          Q("method", "支付方式筛选（CASH/CARD/TRANSFER）。"),
          Q("booking_no", "预订号精确筛选。"), *PQ()],
   permission=FIN_READ_PERM,
   ok=(("200", "支付行数组"),),
   data=s_arr(s_obj({
       "id": s_int(), "folio_id": s_int(), "booking_no": s_str(),
       "provider": s_str(), "provider_ref": s_str(), "method": s_str(),
       "amount": s_str(), "currency": s_str(),
       "status": s_str(), "status_label": s_str(),
       "paid_at": s_nullable(s_str("", format="date-time")),
       "created_at": s_str("", format="date-time"),
   })), errors=E_STAFF)

op("/billing/payments", "post", "财务", "统一收款（前台/财务中心）",
   status="verified",
   body=s_obj({
       "reservation_id": s_int("预订主单 ID（必须属当前物业）。"),
       "amount": s_str("收款金额（字符串十进制，精确到分，>0）。"),
       "method": s_str("支付方式 CASH/CARD/TRANSFER。"),
       "reference": s_str("可选参考号；即服务层 provider_ref 幂等键（≤80 字符）。"),
   }, required=["reservation_id", "amount", "method"]),
   permission="有效员工 + hotelapp.finance_access（BillingService.collect_payment 校验"
              "账单未关闭/金额/方式/应收余额）",
   ok=(("200", "同 reference 幂等重放（返回原支付）"),
       ("201", "收款成功：Payment + PAYMENT 贷方凭证 + 审计 billing.collect")),
   data=s_obj({
       "id": s_int(), "payment_id": s_int(), "folio_id": s_int(),
       "reservation_id": s_int(), "provider_ref": s_str(), "method": s_str(),
       "amount": s_str(), "currency": s_str(), "status": s_str(),
   }), errors=E_STAFF,
   notes="审计事件：billing.collect。账本凭证号 payment:{provider_ref}（全局唯一幂等）。",
   extras={"x-idempotency": {"header": "Idempotency-Key", "required": False, "max_length": 128,
                             "semantics": "api_contract.idempotent 头幂等 + 服务层 reference 幂等；"
                                          "同 reference 不同事实 → 400。"}})

op("/billing/refunds", "get", "财务", "退款列表（申请/审批/执行状态）",
   status="verified",
   query=[Q("status", "退款状态筛选 PENDING/APPROVED/SUCCEEDED/REJECTED。"), *PQ()],
   permission=FIN_READ_PERM,
   ok=(("200", "退款行数组（含审批记录状态）"),),
   data=s_arr(s_obj({
       "id": s_int(), "payment_id": s_int(), "folio_id": s_int(),
       "amount": s_str(), "currency": s_str(), "reason": s_str(),
       "status": s_str(), "status_label": s_str(), "provider_ref": s_str(),
       "requested_by": s_nullable(s_str()), "approved_by": s_nullable(s_str()),
       "executed_by": s_nullable(s_str()), "executed_at": s_nullable(s_str("", format="date-time")),
       "execution_reference": s_nullable(s_str()), "execution_shift_id": s_nullable(s_int()),
       "legacy_workflow": s_bool("历史退款记录，无法证明新流程的申请/审批/执行三方分离。"),
       "approval_status": s_nullable(s_str()),
       "created_at": s_str("", format="date-time"),
       "processed_at": s_nullable(s_str("", format="date-time")),
   })), errors=E_STAFF)

op("/billing/refunds", "post", "财务", "退款申请（双人审批 SoD 第一环）",
   status="verified",
   body=s_obj({
       "payment_id": s_int("原支付流水 ID（必须属当前物业且成功/部分退款）。"),
       "amount": s_str("退款金额（>0，累计不超过原支付金额）。"),
       "reason": s_str("退款原因，1-240 字符（服务强校验）。"),
       "idempotency_key": s_str("幂等键 1-64 字符；缺省取 Idempotency-Key 头。"),
   }, required=["payment_id", "amount", "reason"]),
   permission="有效员工 + hotelapp.finance_access + billing.refund_request"
              "（SoD：申请人≠审批人，审批权 billing.refund_approve 另行持有）",
   ok=(("200", "同幂等键重放（返回原退款申请）"),
       ("201", "退款申请创建：Refund(PENDING) + ApprovalRecord(billing.refund)"
               "+ 审计 billing.refund_request")),
   data=s_obj({
       "id": s_int(), "refund_id": s_int(), "payment_id": s_int(),
       "amount": s_str(), "currency": s_str(), "reason": s_str(),
       "status": s_str(), "provider_ref": s_str(),
       "approval_record_id": s_int(),
   }), errors=E_STAFF,
   notes="审批执行沿用既有 front-desk 退款审批端点（billing.refund_approve），不在本端点重复实现。",
   extras={"x-idempotency": {"header": "Idempotency-Key", "required": False, "max_length": 128,
                             "semantics": "服务层 idempotency_key（body 优先，缺省取头）强校验 1-64 字符；"
                                          "同 key 不同事实 → 400。"}})
op("/billing/refunds/{refund_id}/approve", "post", "财务", "审批退款申请（仅审批，不执行）",
   status="verified", path_params=[("refund_id", "退款申请 ID。")],
   body=s_obj({}, desc="可空请求体；审批不接受“确认已付款”语义。"),
   body_required=False, permission="有效员工 + hotelapp.finance_access + billing.refund_approve",
   ok=(("200", "退款状态变为 APPROVED；不写 REFUND 凭证"),),
   data=s_obj({"refund_id": s_int(), "status": s_str(),
               "approved_by_id": s_int(), "approval_record_id": s_int(),
               "changed": s_bool()}), errors=E_STAFF_CONFLICT,
   notes="ApprovalRecord 审批事实与退款执行完全分离。")

op("/billing/refunds/{refund_id}/reject", "post", "财务", "驳回退款申请",
   status="verified", path_params=[("refund_id", "退款申请 ID。")],
   body=s_obj({"reason": s_str("驳回原因，1-240 字符。")}, required=["reason"]),
   permission="有效员工 + hotelapp.finance_access + billing.refund_approve",
   ok=(("200", "退款状态变为 REJECTED；不写 REFUND 凭证"),),
   data=s_obj({"refund_id": s_int(), "status": s_str(),
               "approved_by_id": s_int(), "changed": s_bool()}),
   errors=E_STAFF_CONFLICT)

op("/billing/refunds/{refund_id}/execute", "post", "财务", "执行已批准退款",
   status="verified", path_params=[("refund_id", "退款申请 ID。")],
   body=s_obj({
       "confirm_refund_paid": s_bool("确认退款已实际支付给住客，必须为 true。"),
       "execution_reference": s_str("支付渠道或人工退款凭据参考号，最多 80 字符。"),
       "idempotency_key": s_str("执行幂等键，1-96 字符；也可使用 Idempotency-Key 头。"),
   }, required=["confirm_refund_paid", "idempotency_key"]),
   permission="有效员工 + hotelapp.finance_access + billing.refund_execute",
   ok=(("201", "退款执行成功，追加唯一 REFUND 凭证并更新 Payment"),
       ("200", "相同执行幂等键重放，不重复入账")),
   data=s_obj({"refund_id": s_int(), "payment_id": s_int(), "status": s_str(),
               "amount": s_str(), "approved_by_id": s_int(),
               "executed_by_id": s_int(), "executed_at": s_str("", format="date-time"),
               "execution_shift_id": s_nullable(s_int()),
               "execution_reference": s_str()}), errors=E_STAFF_CONFLICT,
   notes="申请人、审批人、执行人必须为三个不同账号；现金退款必须关联执行人的开放班次。")

op("/billing/shifts/{shift_id}/movements", "post", "财务", "登记班次现金补入或支出",
   status="verified", path_params=[("shift_id", "收银班次 ID。")],
   body=s_obj({
       "direction": s_str("IN=现金补入；OUT=现金支出/取出。"),
       "amount": s_str("正数金额，Decimal 字符串。"),
       "reason": s_str("业务原因，1-240 字符。"),
       "idempotency_key": s_str("班次内唯一幂等键，1-64 字符。"),
   }, required=["direction", "amount", "reason", "idempotency_key"]),
   permission="有效员工 + hotelapp.finance_access + billing.cash_movement_manage",
   ok=(("201", "现金变动登记成功"), ("200", "同幂等键同事实重放")),
   data=s_obj({"id": s_int(), "shift_id": s_int(), "direction": s_str(),
               "amount": s_str(), "reason": s_str(),
               "created_at": s_str("", format="date-time")}),
   errors=E_STAFF_CONFLICT,
   notes="已关班不可追加；班次应有现金 = 备用金 + 现金收款 - 现金退款 + IN - OUT。")


op("/billing/reversals", "post", "财务", "红字冲销（只追加反向凭证）",
   status="verified",
   body=s_obj({
       "entry_id": s_int("被冲销原凭证 ID（不可为红字凭证）。"),
       "reason": s_str("冲销原因，1-240 字符（服务强校验）。"),
   }, required=["entry_id", "reason"]),
   permission="有效员工 + hotelapp.finance_access + billing.reverse_entry"
              "（F-08：新权限码，不复活已退役的 hotelapp.reverse_checkout）",
   ok=(("200", "同一原凭证重复冲销幂等重放（返回首次反向凭证）"),
       ("201", "冲销成功：追加等额反向 REVERSAL 凭证 + 审计 billing.reverse_entry")),
   data=s_obj({
       "id": s_int(), "reversal_id": s_int(), "original_entry_id": s_int(),
       "void_of_id": s_int(), "amount": s_str("反向金额（原凭证等额反向）。"),
       "currency": s_str(), "reference": s_str("reversal:{原凭证号}。"),
       "entry_type": s_str("REVERSAL"),
   }), errors=E_STAFF,
   notes="原凭证不可变；审计事件：billing.reverse_entry。",
   extras={"x-idempotency": {"header": "Idempotency-Key", "required": False, "max_length": 128,
                             "semantics": "api_contract.idempotent 头幂等 + 服务层 reference 幂等；"
                                          "同一原凭证只能冲销一次，重放返回首次结果。"}})

op("/billing/shifts", "get", "财务", "收银班次列表",
   status="verified",
   query=[Q("status", "班次状态筛选 OPEN/CLOSED/RECONCILED。"),
          Q("mine", "1 时仅返回当前员工本人班次；财务查看全部时也可缩小到本人。"), *PQ()],
   permission="有效员工 + (billing.cashier_shift_manage 或 hotelapp.finance_access)；仅本人班次/本物业全部班次按权限区分",
   ok=(("200", "CashierShift 行数组（备用金/应有/实盘/差异/关班人）"),),
   data=s_arr(s_obj({
       "id": s_int(), "cashier_id": s_int(), "cashier": s_str(),
       "status": s_str(), "status_label": s_str(),
       "opening_cash": s_str(), "expected_cash": s_nullable(s_str()),
       "actual_cash": s_nullable(s_str()), "variance": s_nullable(s_str()),
       "variance_reason": s_str(),
       "opened_at": s_str("", format="date-time"),
       "closed_at": s_nullable(s_str("", format="date-time")),
       "closed_by": s_nullable(s_str()),
   })), errors=E_STAFF)

op("/billing/shifts/open", "post", "财务", "收银开班",
   status="verified",
   body=s_obj({"opening_cash": s_str("开班备用金，十进制字符串，默认 0.00。")},
              desc="body 可空（缺省备用金 0.00）。"),
   permission="有效员工 + billing.cashier_shift_manage（前台/财务/管理岗位专项权）",
   ok=(("201", "开班成功"), ("200", "同收银员已有 OPEN 班次，幂等返回原班次（created=false）")), 
   data=s_obj({"id": s_int(), "status": s_str(),
               "opening_cash": s_str(), 
               "opened_at": s_str("", format="date-time"),
               "created": s_bool("true=新建；false=幂等重放已有 OPEN 班次。")}),
   errors=E_STAFF_CONFLICT,
   notes="BillingService.open_shift 唯一写入口；审计事件 billing.shift_open。",
   extras={"x-idempotency": {"header": "Idempotency-Key", "required": False, "max_length": 128,
                             "semantics": "api_contract.idempotent 头幂等 + 服务层"
                                          "同收银员单 OPEN 班次幂等。"}})

op("/billing/shifts/{shift_id}/close", "post", "财务", "收银关班（交班）",
   status="verified", path_params=[("shift_id", "CashierShift ID。")],
   body=s_obj({"actual_cash": s_str("实收现金盘点额，十进制字符串（必填）。"),
               "variance_reason": s_str("差异说明：actual-opening 差异非零时必填"
                                        "（服务强校验），≤240 字符。")},
              required=["actual_cash"]),
   permission="有效员工 + billing.cashier_shift_manage；跨物业班次一律 404",
   ok=(("200", "关班成功；返回 expected/actual/variance 与差异说明"),),
   data=s_obj({"id": s_int(), "status": s_str(), "status_label": s_str(),
               "opening_cash": s_str(), "expected_cash": s_str(),
               "actual_cash": s_str(), "variance": s_str(),
               "variance_reason": s_str(),
               "closed_at": s_str("", format="date-time")}),
   errors=E_STAFF_CONFLICT,
   notes="BillingService.close_shift 唯一写入口；差异非零必须说明并审计"
         "（billing.shift_close）；重复关班 400。expected_cash 按班次现金事实计算：开班备用金 + 现金收款 - 现金退款 + 现金补入 - 现金支出。",
   extras={"x-idempotency": {"header": "Idempotency-Key", "required": False, "max_length": 128,
                             "semantics": "api_contract.idempotent：同 key 同 body 重放首次响应；"
                                          "班次状态迁移不可重复执行（重复关班 400）。"}})

op("/billing/summary", "get", "财务", "可配置期间财务汇总",
   status="verified",
   query=[Q("from", "起始营业日 YYYY-MM-DD；缺省当前营业日。", "string", False, "2026-11-06", fmt="date"),
          Q("to", "结束营业日 YYYY-MM-DD；缺省当前营业日。", "string", False, "2026-11-06", fmt="date")],
   permission=FIN_READ_PERM + "；期间最长 92 天",
   ok=(("200", "期间收支/收款/退款汇总（BillingService.financial_summary）"),),
   data=s_obj({
       "total_revenue": s_str(), "total_expense": s_str(), "net": s_str(),
       "payment_count": s_int(), "payment_total": s_str(),
       "refund_count": s_int(), "refund_total": s_str(),
       "entry_count": s_int(),
       "period": s_obj({"start": s_str("", format="date"), "end": s_str("", format="date")}),
       "basis": s_obj({"ledger_entries": s_str(), "payments_refunds": s_str()}),
       "property": s_obj({"code": s_str(), "name": s_str()}),
   }), errors=E_STAFF)

# ========== 审批（ApprovalRecord；SoD 双人审批；P0-01/P0-02 加固） ==========
AP_LIST_PERM = ("有效员工（DEFAULT 有效任职）+ billing.approval_view；"
                "持有任一注册动作审批权者见全部，否则仅见本人发起的记录")
AP_REQUEST_PERM = ("有效员工（DEFAULT 有效任职）+ 动作申请权限，经 "
                   "ApprovalActionRegistry 白名单校验"
                   "（billing.refund→billing.refund_request；未知动作 400 零副作用）")
AP_DECISION_PERM = ("有效员工（DEFAULT 有效任职）+ 注册表按 action_code 绑定的审批权限"
                    "（billing.refund_approve / hotelapp.business_date_reopen 等）")

op("/approvals", "get", "审批", "审批记录列表",
   status="partial",
   query=[Q("status", "审批状态筛选。"), *PQ()],
   permission=AP_LIST_PERM,
   ok=(("200", "当前物业审批记录数组"),),
   data=s_arr(_ref("ApprovalRecordDto")), errors=E_STAFF)

op("/approvals/request", "post", "审批", "创建审批申请",
   status="partial",
   body=s_obj({
       "action_code": s_str("审批动作码，如 billing.refund。"),
       "entity_type": s_str("实体类型。"), "entity_id": s_str("实体 ID。"),
       "reason": s_str("申请原因（视图强校验四项均必填）。"),
       "payload": s_nullable(s_obj(desc="附加业务快照。")),
   }, required=["action_code", "entity_type", "entity_id", "reason"]),
   permission=AP_REQUEST_PERM,
   ok=(("201", "审批申请创建成功"), ("200", "相同幂等键重放")),
   data=s_obj({"id": s_int(), "status": s_str(), "created": s_bool()}),
   errors=E_STAFF_CONFLICT,
   extras={"x-idempotency": {"header": "Idempotency-Key", "required": False, "max_length": 128,
                             "semantics": "api_contract.idempotent + ApprovalRecord 幂等键去重；48 小时过期。"}})

op("/approvals/{record_id}/approve", "post", "审批", "批准审批申请",
   status="partial", path_params=[("record_id", "审批记录 ID。")],
   no_body=True,
   permission=AP_DECISION_PERM + "（申请人≠审批人，SoD）",
   ok=(("200", "审批通过"),),
   data=s_obj({"id": s_int(), "status": s_str("APPROVED")}),
   errors=E_STAFF_CONFLICT,
   extras={"x-idempotency": {"header": "Idempotency-Key", "required": False, "max_length": 128,
                             "semantics": "api_contract.idempotent；同一审批只能执行一次。"}})

op("/approvals/{record_id}/reject", "post", "审批", "驳回审批申请",
   status="partial", path_params=[("record_id", "审批记录 ID。")],
   body=s_obj({"reason": s_nullable(s_str("驳回原因；缺省为“驳回”。"))},
              desc="可为空 JSON 对象。"),
   body_required=False,
   permission=AP_DECISION_PERM + "（申请人≠审批人，SoD）",
   ok=(("200", "审批驳回"),),
   data=s_obj({"id": s_int(), "status": s_str("REJECTED")}),
   errors=E_STAFF_CONFLICT,
   extras={"x-idempotency": {"header": "Idempotency-Key", "required": False, "max_length": 128,
                             "semantics": "api_contract.idempotent；同一审批只能执行一次。"}})

# ========== 预订管理 / 审计 ==========
op("/reservations-mgmt", "get", "预订管理", "PMS 预订列表",
   status="partial",
   query=[Q("status", "预订状态筛选。"), Q("q", "预订号/住客姓名搜索。"), *PQ()],
   permission="有效员工 + hotelapp.view_accommodation",
   ok=(("200", "预订主单数组"),),
   data=s_arr(_ref("MgmtReservationListItem")), errors=E_STAFF)

op("/reservations-mgmt/{booking_no}", "get", "预订管理", "PMS 预订详情",
   status="partial", path_params=[("booking_no", "预订号。")],
   permission="有效员工 + hotelapp.view_accommodation",
   ok=(("200", "预订主单 + 房间明细"),),
   data=_ref("MgmtReservationDetail"), errors=E_STAFF)

op("/audit-timeline", "get", "审计", "审计时间线",
   status="partial",
   query=[Q("entity_type", "实体类型精确筛选。"), Q("entity_id", "实体 ID 精确筛选。"),
          Q("action", "动作码包含匹配。"),
          *PQ(page_default=30)],
   permission="有效员工 + core.audit_view（P0-04 定版：审计查看专项权，"
              "与 /audit/events 同门禁；superuser 不自动获得业务授权）",
   ok=(("200", "AuditEvent 数组（倒序）"),),
   data=s_arr(_ref("AuditEventDto")), errors=E_STAFF)

# ========== 员工中心 ==========
op("/staff", "get", "员工中心", "员工列表",
   status="verified", query=[Q("q", "姓名/工号搜索。"), *PQ()],
   permission="有效员工 + identity.staff_manage",
   ok=(("200", "StaffProfile 数组（含角色/任职/账号状态）"),),
   data=s_arr(_ref("StaffDto")), errors=E_STAFF)

op("/staff/{profile_id}", "get", "员工中心", "员工详情",
   status="verified", path_params=[("profile_id", "StaffProfile ID。")],
   permission="有效员工 + identity.staff_manage",
   ok=(("200", "员工档案 + 任职 + 角色授权历史"),),
   data=_ref("StaffDto"), errors=E_STAFF)

# ========== 会员中心 ==========
op("/memberships", "get", "会员中心", "会员列表",
   status="partial",
   query=[Q("status", "会员状态筛选。"), Q("q", "会员号/姓名/手机号搜索。"), *PQ()],
   permission="有效员工 + hotelapp.view_vip",
   ok=(("200", "GuestMembership 数组"),),
   data=s_arr(_ref("MembershipDto")), errors=E_STAFF)

op("/memberships/{membership_id}", "get", "会员中心", "会员详情",
   status="partial", path_params=[("membership_id", "会员关系 ID。")],
   permission="有效员工 + hotelapp.view_vip",
   ok=(("200", "会员详情 + 等级历史"),),
   data=s_obj({**COMPONENTS["MembershipDto"]["properties"],
               **COMPONENTS["MembershipDetailDto"]["properties"]},
              desc="MembershipDto + level_history。"),
   errors=E_STAFF)

op("/memberships/create", "post", "会员中心", "创建会员关系",
   status="partial",
   body=s_obj({
       "guest_id": s_int("本物业客人档案 ID（强校验存在）。"),
       "member_no": s_nullable(s_str("会员号；缺省由服务生成。")),
       "level_code": s_nullable(s_str("等级代码；缺省 STANDARD。")),
       "joined_at": s_nullable(s_str("入会日期 YYYY-MM-DD。", format="date")),
   }, required=["guest_id"]),
   permission="有效员工 + hotelapp.add_vip",
   ok=(("201", "会员关系创建成功"),),
   data=s_obj({"id": s_int(), "member_no": s_str(), "guest_id": s_int(),
               "level_code": s_str(), "status": s_str()}),
   errors=E_STAFF_CONFLICT,
   notes="不按手机号自动合并客人档案。",
   extras={"x-idempotency": {"header": "Idempotency-Key", "required": False, "max_length": 128,
                             "semantics": "api_contract.idempotent：同 key 同 body 重放首次响应；不同 body 409；TTL 24h。"}})

op("/memberships/{membership_id}/level", "post", "会员中心", "变更会员等级",
   status="partial", path_params=[("membership_id", "会员关系 ID。")],
   body=s_obj({"level_code": s_str("新等级代码，1-32 字符（服务强校验）。"),
               "reason": s_str("变更原因，1-240 字符（服务强校验）。")}, required=["level_code", "reason"]),
   permission="有效员工 + hotelapp.change_vip",
   ok=(("200", "等级变更成功；同等级不重复变更"),),
   data=s_obj({"id": s_int(), "level_code": s_str(), "status": s_str()}),
   errors=E_STAFF_CONFLICT,
   notes="写 MembershipLevelHistory 与审计。",
   extras={"x-idempotency": {"header": "Idempotency-Key", "required": False, "max_length": 128,
                             "semantics": "api_contract.idempotent：同 key 同 body 重放首次响应；不同 body 409；TTL 24h。"}})

op("/memberships/{membership_id}/status", "post", "会员中心", "会员暂停/恢复",
   status="verified", path_params=[("membership_id", "会员关系 ID。")],
   body=s_obj({"action": s_str("suspend=暂停（ACTIVE/PENDING→SUSPENDED）；"
                               "restore=恢复（SUSPENDED→ACTIVE）。"),
               "reason": s_str("原因，1-240 字符（服务强校验）。")},
              required=["action", "reason"]),
   permission="有效员工 + hotelapp.change_vip（与等级变更同权；基线建议的独立"
              "membership 专项码随权限矩阵扩展另行登记）",
   ok=(("200", "状态变更成功；同状态重复提交 changed=false（幂等）"),),
   data=s_obj({"id": s_int(), "member_no": s_str(), "status": s_str(),
               "status_label": s_str(), "changed": s_bool()}),
   errors=E_STAFF_CONFLICT,
   notes="基线 2.0 §3.4：写操作经 GuestMembershipService（状态机唯一写入口）；"
         "暂停释放有效会员唯一键，恢复前复核同客人无其他有效会员（冲突 409）；"
         "审计事件 membership.suspend / membership.restore。",
   extras={"x-idempotency": {"header": "Idempotency-Key", "required": False, "max_length": 128,
                             "semantics": "api_contract.idempotent 头幂等 + 服务层同状态幂等。"}})

# ========== 审计与合规中心（基线 2.0 §4.4；读=core.audit_view 唯一门禁） ==========
AUDIT_READ_PERM = ("有效员工（DEFAULT 有效任职）+ core.audit_view（P0-04 专项权，"
                   "migration core.0010 授予店长/值班经理）；QuerySet 强制当前物业过滤，"
                   "跨物业事件一律 404；列表不下发 before/after 载荷")

op("/audit/events", "get", "审计", "审计事件分页检索",
   status="verified",
   query=[Q("action", "动作精确筛选（来自 /audit/actions 字典）。"),
          Q("entity_type", "实体类型精确筛选。"),
          Q("entity_id", "实体 ID 精确筛选。"),
          Q("actor", "操作人用户名精确筛选。"),
          Q("date_from", "created_at 起始自然日 YYYY-MM-DD。", "string", False, "2026-11-01", fmt="date"),
          Q("date_to", "created_at 截止自然日 YYYY-MM-DD。", "string", False, "2026-11-06", fmt="date"),
          *PQ()],
   permission=AUDIT_READ_PERM + "；meta.retention_note 说明留存口径（保留期限待业务确认）",
   ok=(("200", "当前物业审计事件摘要数组（不含 before/after）"),),
   data=s_arr(s_obj({
       "id": s_int(), "action": s_str(), "action_label": s_str("操作类型中文标签。"),
       "entity_type": s_str(), "entity_type_label": s_str("业务实体中文标签。"),
       "entity_id": s_str(), "actor": s_nullable(s_str("操作人登录账号；系统操作为 null。")),
       "actor_label": s_str("员工姓名（登录账号）或系统。"),
       "reason": s_nullable(s_str()),
       "is_sensitive": s_bool("SPECIAL_PERMISSIONS 相关敏感动作标记。"),
       "created_at": s_str("", format="date-time"),
   }), desc="审计事件摘要（action/entity/actor/时间/reason）"), errors=E_STAFF)

op("/audit/events/{event_id}", "get", "审计", "审计事件详情（before/after 完整载荷）",
   status="verified", path_params=[("event_id", "AuditEvent ID。")],
   permission=AUDIT_READ_PERM + "；揭示类事件只存 masked 值（写入侧脱敏纪律）",
   ok=(("200", "事件全量字段：before/after JSON、reason、request_id、ip、user_agent"),),
   data=s_obj({
       "id": s_int(), "action": s_str(), "action_label": s_str("操作类型中文标签。"),
       "entity_type": s_str(), "entity_type_label": s_str("业务实体中文标签。"),
       "entity_id": s_str(), "actor": s_nullable(s_str("操作人登录账号；系统操作为 null。")),
       "actor_label": s_str("员工姓名（登录账号）或系统。"),
       "reason": s_nullable(s_str()),
       "before": s_nullable(s_obj(desc="变更前载荷（敏感字段写入前脱敏）。", additional=True)),
       "after": s_nullable(s_obj(desc="变更后载荷（敏感字段写入前脱敏）。", additional=True)),
       "request_id": s_nullable(s_str()), "ip": s_nullable(s_str("", format="ipv4")),
       "user_agent": s_nullable(s_str()),
       "is_sensitive": s_bool(),
       "created_at": s_str("", format="date-time"),
       "property": s_nullable(s_obj({"code": s_str(), "name": s_str()})),
   }), errors=E_STAFF)

op("/audit/summary", "get", "审计", "审计统计（近 7 天/30 天、Top 操作者、敏感动作）",
   status="verified",
   permission=AUDIT_READ_PERM + "；单查询双窗口条件聚合（30 天窗口内拆 7 天计数）",
   ok=(("200", "按 action 分组计数、Top 操作者（近 30 天前 5）、敏感动作单独计数"),),
   data=s_obj({
       "window": s_obj({"now": s_str("", format="date-time"),
                        "start_7d": s_str("", format="date-time"),
                        "start_30d": s_str("", format="date-time")}),
       "total_7d": s_int(), "total_30d": s_int(),
       "by_action": s_arr(s_obj({"action": s_str(), "count_7d": s_int(),
                                 "count_30d": s_int()})),
       "top_actors": s_arr(s_obj({"actor": s_str(), "count_7d": s_int(),
                                  "count_30d": s_int()})),
       "sensitive": s_obj({
           "count_7d": s_int(), "count_30d": s_int(),
           "by_action": s_arr(s_obj({"action": s_str(), "count_7d": s_int(),
                                     "count_30d": s_int()})),
           "catalog_note": s_str("敏感动作清单见 sensitive_actions_catalog"
                                 "（SPECIAL_PERMISSIONS 相关已落审计动作）。"),
       }),
       "sensitive_actions_catalog": s_arr(s_str()),
       "property": s_obj({"code": s_str(), "name": s_str()}),
   }), errors=E_STAFF)

op("/audit/actions", "get", "审计", "审计动作字典（去重清单 + 计数，供筛选下拉）",
   status="verified",
   permission=AUDIT_READ_PERM,
   ok=(("200", "当前物业出现过的 action 数组（按计数倒序）"),),
   data=s_arr(s_obj({"action": s_str(), "count": s_int(),
                     "is_sensitive": s_bool()})), errors=E_STAFF)

# ========== 清洁与维修工作台（基线 2.0 §4.3） ==========
HK_ROOM_DTO = s_obj({
    "id": s_int(), "number": s_str(), "floor": s_str(),
    "room_type": s_obj({"id": s_int(), "code": s_str(), "name": s_str()}),
    "occupancy_status": s_str(), "occupancy_label": s_str(),
    "housekeeping_status": s_str("CLEAN/DIRTY/CLEANING/INSPECTING。"),
    "housekeeping_label": s_str(), "maintenance_status": s_str(),
    "maintenance_label": s_str(),
    "is_sellable_now": s_bool("启用+空闲+净房+无维修（Room.is_sellable_now）。"),
    "source_system": s_str(),
})

op("/housekeeping/rooms", "get", "清洁与维修", "清洁/维修工作清单",
   status="verified",
   query=[Q("floor", "楼层筛选。"), Q("housekeeping", "清洁状态筛选 CLEAN/DIRTY/CLEANING/INSPECTING。"),
          Q("maintenance", "维修状态筛选 NORMAL/OUT_OF_ORDER/OUT_OF_SERVICE。"),
          Q("occupancy", "占用状态筛选 VACANT/OCCUPIED。"), *PQ()],
   permission="有效员工 + inventory.housekeeping_view",
   ok=(("200", "三维房态行数组（floor,number,id 稳定排序）"),),
   data=s_arr(HK_ROOM_DTO), errors=E_STAFF)

op("/housekeeping/summary", "get", "清洁与维修", "今日清洁统计",
   status="verified",
   permission="有效员工 + inventory.housekeeping_view",
   ok=(("200", "各清洁/维修状态计数、待放行数、楼层分布与今日审计流转量"),),
   data=s_obj({
       "housekeeping": s_obj(desc="清洁状态计数映射。"),
       "maintenance": s_obj(desc="维修状态计数映射。"),
       "pending_release": s_int("INSPECTING 待放行数。"),
       "sellable_now": s_int("当前可售房数（空闲+净房+无维修）。"),
       "total_rooms": s_int(), "by_floor": s_arr(s_obj(desc="按楼层分组计数。")),
       "today": s_obj({"housekeeping_changes": s_int("今日清洁流转（AuditEvent 计数）。"),
                        "released": s_int("今日净房放行数。"),
                        "reworked": s_int("今日检查退回数。"),
                        "maintenance_changes": s_int("今日维修登记数。")}),
       "labels": s_obj(desc="状态中文标签映射。"),
   }), errors=E_STAFF)

op("/housekeeping/rooms/{room_id}/status", "post", "清洁与维修", "清洁状态流转",
   status="verified", path_params=[("room_id", "房间 ID。")],
   body=s_obj({"status": s_str("目标清洁状态 CLEANING/INSPECTING/CLEAN/DIRTY。"),
                "notes": s_nullable(s_str("备注（≤240 字符，入审计）。"))},
              required=["status"]),
   permission="有效员工 + inventory.housekeeping_update；→CLEAN 净房放行另需专项 hotelapp.room_release_ready（服务层+API 层双重校验）",
   ok=(("200", "流转成功（changed=false 表示幂等空操作）"),),
   data=s_obj({**HK_ROOM_DTO["properties"], "changed": s_bool()}),
   errors=E_STAFF_CONFLICT,
   notes="链式规则：DIRTY→CLEANING→INSPECTING→CLEAN；INSPECTING→DIRTY 检查退回；"
         "跳步 409 BUSINESS_CONFLICT。每次实际变更落 AuditEvent "
         "housekeeping.status_changed。占用/维修维度不在本端点修改。",
   extras={"x-audit-events": ["housekeeping.status_changed"]})

op("/housekeeping/rooms/{room_id}/maintenance", "post", "清洁与维修", "维修状态登记",
   status="verified", path_params=[("room_id", "房间 ID。")],
   body=s_obj({"status": s_str("目标维修状态 OUT_OF_ORDER/OUT_OF_SERVICE/NORMAL。"),
                "reason": s_str("登记原因，1-240 字符（服务强校验）。")},
              required=["status", "reason"]),
   permission="有效员工 + inventory.maintenance_manage（值班经理/店长口径；与 hotelapp.no_show_handle 完全分离）",
   ok=(("200", "登记成功（changed=false 表示幂等空操作）"),),
   data=s_obj({**HK_ROOM_DTO["properties"], "changed": s_bool()}),
   errors=E_STAFF_CONFLICT,
   notes="恢复（→NORMAL）不联动清洁状态：房间仍脏需继续清洁链。"
         "每次实际变更落 AuditEvent maintenance.status_changed。",
   extras={"x-audit-events": ["maintenance.status_changed"]})

# ========== 报表分析（基线 2.0 §4.5；只读聚合，不作为第二账本） ==========
RPT_FROM_TO = [
    Q("from", "起始日期 YYYY-MM-DD；缺省最近 7 天（含物业时区今天）。", "string", False,
      "2026-11-01", fmt="date"),
    Q("to", "结束日期 YYYY-MM-DD；缺省物业时区今天。", "string", False,
      "2026-11-06", fmt="date"),
]
RPT_RANGE_NOTE = "区间最长 92 天（防全表扫描）；响应 data.metadata 携带统计口径说明" \
                 "（物业/日期口径/状态范围/币种/生成时间）；空区间返回空序列不报错。" \
                 "纯只读数据库聚合，不新增模型/快照落库。"

op("/reports/occupancy", "get", "报表分析", "入住率报表（按日）",
   status="verified", query=RPT_FROM_TO,
   permission="有效员工 + hotelapp.view_accommodation",
   ok=(("200", "按日序列（可售房数/已售间夜/入住率%）+ 期间汇总 + 口径 metadata"),),
   data=s_obj({
       "series": s_arr(s_obj({
           "date": s_str("", format="date"),
           "total_rooms": s_int(), "out_of_service": s_int(), "held": s_int(),
           "sellable_room_nights": s_int(), "sold_room_nights": s_int(),
           "occupancy_pct": s_nullable(s_str("两位小数百分比字符串；可售间夜为 0 时 null。")),
       })),
       "summary": s_obj({"sellable_room_nights": s_int(), "sold_room_nights": s_int(),
                          "occupancy_pct": s_nullable(s_str())}),
       "metadata": s_obj(desc="统计口径（P0-07 模式）。"),
   }), errors=E_STAFF,
   notes="OCC = Σsold / Σ(total − out_of_service)：RoomTypeInventory 逐日库存快照口径，"
         "停用房从分母剔除；held（待支付冻结）计入分母不计入分子。" + RPT_RANGE_NOTE)

op("/reports/adr", "get", "报表分析", "ADR 报表（按日）",
   status="verified", query=RPT_FROM_TO,
   permission="有效员工 + hotelapp.finance_access（收入类报表，与财务中心同门禁）",
   ok=(("200", "按日序列（房费收入/已售间夜/ADR）+ 期间汇总 + 口径 metadata"),),
   data=s_obj({
       "series": s_arr(s_obj({
           "date": s_str("", format="date"),
           "room_revenue": s_str(), "sold_room_nights": s_int(),
           "adr": s_nullable(s_str("两位小数字符串；当日无已售间夜时 null。")),
       })),
       "summary": s_obj({"currency": s_str(), "room_revenue": s_str(),
                          "sold_room_nights": s_int(),
                          "adr": s_nullable(s_str()),
                          "other_currencies": s_arr(s_obj(desc="其他币种房费合计（不相加）。"))}),
       "metadata": s_obj(desc="统计口径（P0-07 模式）。"),
   }), errors=E_STAFF,
   notes="ADR = 房费收入 / 已售间夜：分子为 LedgerEntry ROOM_CHARGE 及其红字冲销"
         "（REVERSAL→ROOM_CHARGE）带符号合计（不含押金/收款/调整）；分母为 "
         "RoomTypeInventory.sold。仅对物业结算币种计算 ADR，其他币种单列不相加。"
         + RPT_RANGE_NOTE)

op("/reports/revenue", "get", "报表分析", "收入报表（按营业日/凭证类型分组）",
   status="verified",
   query=[*RPT_FROM_TO,
          Q("group_by", "分组维度：day（按营业日，默认）或 type（按凭证类型）。", "string",
            False, "day")],
   permission="有效员工 + hotelapp.finance_access（收入类报表，与财务中心同门禁）",
   ok=(("200", "分组序列（收入/支出/净额/凭证数）+ 期间汇总 + 口径 metadata"),),
   data=s_obj({
       "group_by": s_str("day / type。"),
       "series": s_arr(s_obj(desc="day：{business_date,revenue,expense,net,entries}；"
                                  "type：另含 entry_type/entry_type_label。")),
       "summary": s_obj({"currency": s_str(), "total_revenue": s_str(),
                          "total_expense": s_str(), "net": s_str(),
                          "entries": s_int(),
                          "other_currencies": s_arr(s_obj(desc="其他币种合计（不相加）。"))}),
       "metadata": s_obj(desc="统计口径（P0-07 模式）。"),
   }), errors=E_STAFF,
   notes="LedgerEntry 口径：收入=正数凭证合计；支出=负数凭证合计的绝对值"
         "（收款/押金/退款/红字冲销等负向凭证，账本资金变动口径，非损益口径）；"
         "净额=全部凭证带符号合计（已体现冲销关系）。主序列仅物业结算币种。"
         + RPT_RANGE_NOTE)

op("/reports/guests", "get", "报表分析", "客源报表（新客/回头客/来源分布）",
   status="verified", query=RPT_FROM_TO,
   permission="有效员工 + hotelapp.view_accommodation（仅聚合计数，不输出客人 PII）",
   ok=(("200", "新客/回头客/预订计数 + 来源分布 + 口径 metadata"),),
   data=s_obj({
       "summary": s_obj({"new_guests": s_int(), "returning_guests": s_int(),
                          "reservations": s_int(), "unique_guests": s_int()}),
       "by_source": s_arr(s_obj({"source": s_str(), "source_label": s_str(),
                                  "reservations": s_int()})),
       "metadata": s_obj(desc="统计口径（P0-07 模式）。"),
   }), errors=E_STAFF,
   notes="新客 = 期间内建档（Guest.created_at 自然日）客人数；回头客 = 期间内有预订"
         "且本物业历史预订总数 ≥ 2 的去重客人数（含已取消预订）；来源分布按期间内"
         "创建预订的 Reservation.source 分组。" + RPT_RANGE_NOTE)

op("/reports/housekeeping", "get", "报表分析", "清洁效率报表（事件按日计数）",
   status="verified", query=RPT_FROM_TO,
   permission="有效员工 + hotelapp.view_accommodation（运营事件口径，不含金额与 PII）",
   ok=(("200", "按自然日序列（状态流转次数）+ 期间汇总 + 口径 metadata"),),
   data=s_obj({
       "series": s_arr(s_obj({"date": s_str("", format="date"),
                               "status_changes": s_int()})),
       "summary": s_obj({"total_changes": s_int(), "rooms_involved": s_int(),
                          "avg_changes_per_day": s_nullable(s_str())}),
       "metadata": s_obj(desc="统计口径（P0-07 模式）。"),
   }), errors=E_STAFF,
   notes="事件口径：AuditEvent（action=housekeeping.status_changed，含净房放行与"
         "检查退回）按 created_at 自然日分组计数；为运营趋势参考，非清洁任务完成率。"
         + RPT_RANGE_NOTE)

# ========== 通知与待办（基线 2.0 §4.6；站内通知，无第三方渠道） ==========
NT_PERM = ("有效员工 + 当前物业有效任职（无专项权限码，§4.6 全员工作入口；"
           "读取范围=当前物业 + 定向本人 或 岗位广播组∈当前有效任职组）")

op("/notifications", "get", "通知与待办", "我的通知分页列表",
   status="verified",
   query=[Q("category", "类别筛选 reservation/housekeeping/approval/audit/system。"),
          Q("severity", "级别筛选 info/warning/critical。"),
          Q("is_read", "已读筛选 true/false。"),
          *PQ()],
   permission=NT_PERM,
   ok=(("200", "我的通知数组（-created_at,-id 稳定排序）+ meta.pagination"),),
   data=s_arr(s_obj({
       "id": s_int(), "category": s_str(), "category_label": s_str(),
       "severity": s_str(), "severity_label": s_str(),
       "title": s_str(), "body": s_str(), "link": s_nullable(s_str("站内相对路径。")),
       "is_read": s_bool(), "read_at": s_nullable(s_str("", format="date-time")),
       "created_at": s_str("", format="date-time"),
       "expires_at": s_nullable(s_str("", format="date-time")),
       "audience": s_obj({"type": s_str("staff=定向本人 / role_group=岗位广播。"),
                          "role_group": s_nullable(s_str())}),
   }), desc="通知是提示不是业务状态权威；meta.refresh_note 说明实时派生口径"),
   errors=E_STAFF,
   notes="§4.6：调岗/离职后旧岗位广播自动不可见（按当前有效任职动态计算）.")

op("/notifications/unread-count", "get", "通知与待办", "我的未读通知计数",
   status="verified", permission=NT_PERM,
   ok=(("200", "未读总数 + 按类别分布"),),
   data=s_obj({"unread_count": s_int(), "by_category": s_obj(desc="类别→未读数。")}),
   errors=E_STAFF, notes="侧边栏未读徽标数据源（与列表同一读取范围）。")

op("/notifications/{notification_id}/read", "post", "通知与待办", "标记单条通知已读",
   status="verified", path_params=[("notification_id", "Notification ID。")],
   no_body=True, permission=NT_PERM,
   ok=(("200", "标记成功；重复调用幂等（不覆盖首次 read_at）"),),
   data=s_obj(desc="标记后的通知 DTO。"), errors=E_STAFF,
   notes="范围外（他物业/非我的定向/非我的岗位广播）一律 404。")

op("/notifications/read-all", "post", "通知与待办", "我的未读通知全部标记已读",
   status="verified", no_body=True, permission=NT_PERM,
   ok=(("200", "返回本次标记条数 marked_read"),),
   data=s_obj({"marked_read": s_int("仅统计我的范围内的未读。")}),
   errors=E_STAFF)

op("/notifications/refresh", "post", "通知与待办", "触发当前物业运营待办实时重算",
   status="verified", no_body=True, permission=NT_PERM,
   ok=(("200", "对账摘要 active/created/updated/resolved/by_code"),),
   data=s_obj({"active": s_int("当前活跃待办行数。"), "created": s_int(),
                "updated": s_int(), "resolved": s_int("自动销项（置已读）条数。"),
                "by_code": s_obj(desc="待办来源代码→本物业行数。")}),
   errors=E_STAFF,
   notes="§4.6 首期口径：实时派生 + upsert（无 celery）；按物业隔离；"
         "员工可触发；重复刷新幂等（dedupe_key 去重）；"
         "已解决项自动置已读；通知生成不产生 AuditEvent。")

# ========== 文档 ==========
op("/openapi.json", "get", "文档", "获取 OpenAPI 规范",
   status="verified", auth=False, envelope="raw",
   ok=(("200", "OpenAPI 3.1 JSON 文档"),),
   data=s_obj(desc="本文档本身。"))


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def _operation_id(method, path):
    slug = re.sub(r"[^0-9a-zA-Z]+", "_", path).strip("_").lower()
    return f"{method}_{slug}"


def _envelope_data(entry):
    """按封套类型生成成功响应 schema。"""
    data = entry["data"] or s_obj()
    if entry["envelope"] == "api":
        return s_obj({"data": data, "meta": _ref("ResponseMeta")},
                     required=["data", "meta"],
                     desc="{data, meta} 统一成功封套（apps.core.api_contract.api_response）。")
    if entry["envelope"] == "fd":
        return s_obj({"data": data}, required=["data"],
                     desc="{data, ...} 前台封套；列表附 meta 分页，写操作附 created/changed 标志。")
    return data


def _render_operation(entry):
    method = entry["method"]
    params = []
    for name, desc in entry["path_params"]:
        params.append(_path_param(name, desc))
    params.extend(entry["query"])
    extras = dict(entry["extras"])
    idem = extras.get("x-idempotency")
    if idem and method == "post":
        req = bool(idem.get("required"))
        max_len = idem.get("max_length", 128)
        key_param = P(
            "Idempotency-Key",
            f"幂等键（{'必填' if req else '可选'}，1-{max_len} 字符）。",
            "string", "header", req)
        key_param["schema"]["maxLength"] = max_len
        params.append(key_param)
    if "x-booking_token_header_param" in extras:
        params.append(extras.pop("x-booking_token_header_param"))
    signature = extras.get("x-signature")
    if signature and method == "post":
        params.append(P(signature.get("header", "X-Signature"),
                        "回调报文 HMAC 签名（verify_mock_callback 校验，失败 401）。",
                        "string", "header", True))
    if method == "post" and not entry["path"].endswith("/payments/callback"):
        params.append(CSRF_PARAM)

    responses = {}
    for code, desc in entry["ok"]:
        responses[code] = {"description": desc,
                           "content": {"application/json": {"schema": _envelope_data(entry)}}}
    public = not entry["auth"]
    for code in entry["errors"]:
        responses[code] = _error_response(code, public=public)

    permission = entry["permission"]
    notes = entry["notes"]
    if permission:
        notes = (notes + "\n\n" if notes else "") + "权限要求：" + permission + "。"
    if entry["status"] == "partial":
        notes = (notes + "\n\n" if notes else "") + \
            "契约状态 partial：请求/响应字段已按视图与服务源码核实，端点级 HTTP 测试待补齐。"
    elif entry["status"] == "verified":
        notes = (notes + "\n\n" if notes else "") + \
            "契约状态 verified：字段与错误结构经源码核实并有端点级测试覆盖。"

    item = {
        "tags": [entry["tag"]],
        "summary": entry["summary"],
        "description": notes or entry["summary"],
        "operationId": _operation_id(method, entry["path"]),
        "responses": responses,
        "x-contract-status": entry["status"],
        "x-authentication": "匿名（公开预订/支付）" if public else "Django Session Cookie + 员工任职",
    }
    if permission:
        item["x-required-permission"] = permission
    item.update(extras)
    if params:
        item["parameters"] = params
    if entry["body"] is not None:
        item["requestBody"] = {
            "required": entry["body_required"],
            "content": {"application/json": {"schema": entry["body"]}},
        }
    if entry["no_body"]:
        item["x-no-request-body"] = True
    if entry["auth"]:
        item["security"] = COOKIE_SECURITY
    else:
        item["security"] = []
    return item


def build_openapi():
    paths = {}
    seen_ids = {}
    for entry in CONTRACTS:
        full_path = BASE + entry["path"]
        op_id = _operation_id(entry["method"], entry["path"])
        if op_id in seen_ids:
            raise ValueError(f"operationId 重复：{op_id}")
        seen_ids[op_id] = True
        paths.setdefault(full_path, {})[entry["method"]] = _render_operation(entry)
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "PMS2 酒店管理系统 API",
            "version": "2.1.0",
            "description": (
                "PMS2 模块化单体 API 契约（方案 §9.8）。只包含当前注册的 JSON API；"
                "HTML 页面、表单动作和历史页面入口不作为 REST API。\n\n"
                "契约状态（x-contract-status）：verified = 字段经源码核实且有端点级 HTTP 测试；"
                "partial = 字段经源码核实但端点级测试待补齐；planned = 规划未实现（当前不存在）。\n\n"
                "安全模型：员工端统一 Django Session Cookie（登录入口为 POST /login/ HTML Form，"
                "不存在 JSON 登录接口），所有写操作需要 X-CSRFToken；公开预订/支付为匿名访问，"
                "凭 Booking Access Token 业务凭据 + §6.3 限流 + 审计。"
            ),
        },
        "servers": [{"url": "/"}],
        "tags": [{"name": name} for name in TAGS],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "PMS2Session": {
                    "type": "apiKey", "in": "cookie", "name": "sessionid",
                    "description": "Django Session 登录 Cookie（CSRF_USE_SESSIONS=True）。"
                                   "员工登录入口为 POST /login/ HTML Form，不是 JSON API。",
                },
            },
            "schemas": COMPONENTS,
        },
    }


@require_GET
def openapi_json(request):
    return JsonResponse(build_openapi(), json_dumps_params={"ensure_ascii": False})


@require_GET
def swagger_ui(request):
    html = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>PMS2 Swagger UI</title><link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css"></head><body><div id="swagger-ui"></div><script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script><script>window.onload=()=>SwaggerUIBundle({url:'/api/v1/openapi.json',dom_id:'#swagger-ui',deepLinking:true,persistAuthorization:true,displayRequestDuration:true});</script></body></html>"""
    return HttpResponse(html)


@require_GET
def redoc_ui(request):
    html = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>PMS2 ReDoc</title></head><body><redoc spec-url="/api/v1/openapi.json"></redoc><script src="https://unpkg.com/redoc@2/bundles/redoc.standalone.js"></script></body></html>"""
    return HttpResponse(html)
