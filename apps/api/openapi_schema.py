# -*- coding: utf-8 -*-
"""PMS2 OpenAPI 3.1 contract.

This specification deliberately distinguishes implemented API routes from page/form
workflows. The contract status extension marks operations whose complete field-level
DTO contract still requires source/test verification.
"""
from django.http import JsonResponse, HttpResponse
from django.views.decorators.http import require_GET

BASE = "/api/v1"
COMMON_ERRORS = {
    "400": {"description": "请求参数或 JSON 请求体校验失败"},
    "401": {"description": "未登录或认证凭据无效"},
    "403": {"description": "当前员工/角色无权执行此操作"},
    "404": {"description": "资源不存在或不属于当前物业"},
    "409": {"description": "业务状态冲突、库存冲突或幂等冲突"},
    "429": {"description": "请求频率超限；请遵循 Retry-After"},
    "500": {"description": "服务器内部错误"},
}
JSON = {"application/json": {"schema": {"type": "object", "additionalProperties": True}}}
COOKIE_SECURITY = [{"PMS2Session": []}]
CSRF = {"name": "X-CSRFToken", "in": "header", "required": False,
        "description": "Django CSRF token；Session 写操作需要有效 CSRF。", "schema": {"type": "string"}}

def _param(name, location="path", typ="string", required=True, description=""):
    return {"name": name, "in": location, "required": required,
            "description": description, "schema": {"type": typ}}

def _operation(summary, tag, method, *, path_params=(), query=(), body=None,
               auth=True, permission="按对应视图的 AuthorizationService/员工任职门禁校验",
               success="请求成功", success_code="200", notes="", deprecated=False):
    params = [_param(p) for p in path_params]
    params += [_param(q[0], "query", q[1], q[2], q[3] if len(q) > 3 else "") for q in query]
    if method == "post":
        params.append(CSRF)
    responses = {success_code: {"description": success, "content": {"application/json": {"schema": {"type": "object", "additionalProperties": True}}}}}
    responses.update({code: {"description": value["description"], "content": {"application/json": {"schema": {"type": "object", "additionalProperties": True}}}} for code, value in COMMON_ERRORS.items()})
    item = {
        "tags": [tag], "summary": summary,
        "description": (notes + "\n\n" if notes else "") +
            "权限要求：" + permission + "。字段级契约状态：部分接口 DTO 仍需逐字段核验；以源码和契约测试为准。",
        "operationId": tag.lower().replace(" ", "_") + "_" + summary.replace(" ", "_"),
        "responses": responses,
        "x-contract-status": "implemented-route-field-contract-partial",
        "x-authentication": "Django Session Cookie" if auth else "匿名/业务凭据",
        "x-permission-boundary": permission,
    }
    if method == "get" and tag == "公开预订" and summary == "使用 Booking Access Token 查询预订":
        params.append(_param("X-Booking-Token", "header", "string", False, "Booking Access Token"))
    if params:
        item["parameters"] = params
    if body:
        item["requestBody"] = {"required": True, "content": {"application/json": {"schema": body}}}
    if auth:
        item["security"] = COOKIE_SECURITY
    if deprecated:
        item["deprecated"] = True
    return item

def _obj(required=(), properties=None):
    return {"type": "object", "required": list(required),
            "properties": properties or {}, "additionalProperties": False}

def build_openapi():
    paths = {}
    def add(path, method, *args, **kwargs):
        paths[BASE + path] = paths.get(BASE + path, {})
        paths[BASE + path][method] = _operation(*args, method=method, **kwargs)

    # Public booking: anonymous access, Booking Access Token is a business credential.
    add("/availability", "get", "查询房型库存与价格", "公开预订", auth=False,
        query=[("check_in","string",True,"入住日期 YYYY-MM-DD"),("check_out","string",True,"离店日期 YYYY-MM-DD"),
               ("rooms","integer",False,"房间数，1-10"),("room_type_id","integer",False,"可选房型 ID")],
        success="物业、日期、房间数及可售房型列表")
    add("/reservations", "post", "创建客房预订", "公开预订", auth=False, success_code="201",
        success="预订创建成功；幂等重放返回 200",
        notes="支持 Idempotency-Key 请求头。首次创建响应一次性返回 access_token，顾客须自行保存。",
        body=_obj(["full_name","phone","room_type_id","check_in","check_out"], {
            "full_name":{"type":"string","maxLength":80},"phone":{"type":"string","maxLength":24},
            "room_type_id":{"type":"integer"},"rate_plan_id":{"type":"integer"},
            "check_in":{"type":"string","format":"date"},"check_out":{"type":"string","format":"date"},
            "rooms":{"type":"integer","minimum":1,"maximum":10,"default":1},
            "adults":{"type":"integer","minimum":1,"maximum":10,"default":1},
            "children":{"type":"integer","minimum":0,"maximum":10,"default":0}}))
    add("/reservations/{booking_no}", "get", "使用 Booking Access Token 查询预订", "公开预订", auth=False,
        path_params=["booking_no"], query=[("token","string",False,"Booking Access Token")],
        success="脱敏预订详情；不返回完整手机号、证件号或支付内部流水",
        notes="必须提供首次创建预订时返回的 Booking Access Token；不再使用手机号作为查询凭据。")
    add("/reservations/{booking_no}/cancel", "post", "取消待支付预订", "公开预订", auth=False,
        path_params=["booking_no"], success="取消状态及 changed 标志",
        body=_obj(["token"], {"token":{"type":"string","description":"Booking Access Token"}}))
    add("/payments/intents", "post", "创建本地模拟支付意图", "支付", auth=False, success_code="201",
        success="模拟支付单信息；仅开发/测试环境开放",
        notes="生产环境拒绝模拟支付。请求使用 Booking Access Token，不使用手机号。",
        body=_obj(["booking_no","token"], {"booking_no":{"type":"string"},"token":{"type":"string"}}))
    add("/payments/callback", "post", "处理本地模拟支付回调", "支付", auth=False,
        success="回调处理状态；仅 dev/test 可用",
        notes="需要 X-Signature 签名；生产环境禁用。",
        body=_obj(["provider_ref","amount","status"], {"provider_ref":{"type":"string"},
            "amount":{"type":"string"},"status":{"type":"string","enum":["SUCCESS","FAILED"]}}))
    
    # Front desk query and state-changing operations.
    fd_get = [
        ("/front-desk/overview","前台总览",[],[("date","string",False,"业务日期 YYYY-MM-DD")]),
        ("/front-desk/arrivals","抵店列表",[],[("date","string",False,"抵店日期"),("q","string",False,"搜索词"),("assigned","string",False,"排房筛选"),("page","integer",False,"页码"),("page_size","integer",False,"每页数量")]),
        ("/front-desk/departures","离店列表",[],[("date","string",False,"离店日期"),("q","string",False,"搜索词")]),
        ("/front-desk/in-house","在住列表",[],[("q","string",False,"搜索词"),("room_type_id","integer",False,"房型 ID"),("floor","string",False,"楼层")]),
        ("/front-desk/reservations","预订列表",[],[("q","string",False,"搜索词"),("status","string",False,"预订状态"),("source","string",False,"来源")]),
        ("/front-desk/reservations/{booking_no}","预订详情",["booking_no"],[]),
        ("/front-desk/rooms","房间列表",[],[("floor","string",False,"楼层"),("readiness","string",False,"可用状态")]),
        ("/front-desk/rooms/assignable","可分配房间",[],[("reservation_room_id","integer",True,"预订房间明细 ID")]),
        ("/front-desk/folios/{booking_no}","预订账单详情",["booking_no"],[]),
        ("/front-desk/legacy-reservations","历史预订列表",[],[("q","string",False,"搜索词"),("status","string",False,"状态")]),
        ("/front-desk/legacy-reservations/{legacy_id}","历史预订详情",["legacy_id"],[]),
        ("/front-desk/stays/{stay_id}/available-rooms","在住订单可换房列表",["stay_id"],[]),
    ]
    for p,s,pp,qq in fd_get:
        add(p,"get",s,"前台接待",path_params=pp,query=qq,
            permission="有效员工任职 + hotelapp.view_accommodation")
    fd_post = [
        ("/front-desk/reservation-rooms/{reservation_room_id}/assign","分配房间",["reservation_room_id"],_obj(["room_id"],{"room_id":{"type":"integer"}})),
        ("/front-desk/reservation-rooms/{reservation_room_id}/check-in","办理入住",["reservation_room_id"],_obj()),
        ("/front-desk/reservations/{booking_no}/payments","收取预订款项",["booking_no"],_obj(["amount","method"],{"amount":{"type":"string"},"method":{"type":"string"}})),
        ("/front-desk/stays/{stay_id}/check-out","办理退房",["stay_id"],_obj()),
        ("/front-desk/stays/{stay_id}/extend","延长入住",["stay_id"],_obj(["new_check_out"],{"new_check_out":{"type":"string","format":"date"},"reason":{"type":"string"}})),
        ("/front-desk/stays/{stay_id}/change-room","在住换房",["stay_id"],_obj(["room_id"],{"room_id":{"type":"integer"},"reason":{"type":"string"}})),
        ("/front-desk/reservation-rooms/{reservation_room_id}/no-show","标记 No-show",["reservation_room_id"],_obj()),
        ("/front-desk/reservations/{booking_no}/deposits","收取押金",["booking_no"],_obj(["amount","method"],{"amount":{"type":"string"},"method":{"type":"string"}})),
        ("/front-desk/payments/{payment_id}/refunds","申请退款",["payment_id"],_obj(["amount","reason"],{"amount":{"type":"string"},"reason":{"type":"string"}})),
        ("/front-desk/refunds/{refund_id}/approve","审批退款",["refund_id"],_obj(["confirm_refund_paid"],{"confirm_refund_paid":{"type":"boolean"}})),
    ]
    for p,s,pp,b in fd_post:
        add(p,"post",s,"前台接待",path_params=pp,body=b,success="操作结果",
            permission="有效员工任职 + hotelapp.view_accommodation + hotelapp.change_accommodation")
    
    # Night audit and emergency access.
    na = [
        ("/night-audit/preview","get","预览夜审",[],None),
        ("/night-audit/run","post","执行夜审",[],_obj()),
        ("/night-audit/business-dates/{business_date_id}/reopen-request","post","申请重开营业日",["business_date_id"],_obj(["reason"],{"reason":{"type":"string"}})),
        ("/night-audit/business-dates/{business_date_id}/approvals/{approval_id}/approve","post","审批重开营业日",["business_date_id","approval_id"],_obj()),
        ("/night-audit/business-dates/{business_date_id}/break-glass","post","申请紧急访问",["business_date_id"],_obj(["password","reason"],{"password":{"type":"string","format":"password"},"reason":{"type":"string"}})),
        ("/night-audit/business-dates/{business_date_id}/break-glass/{record_id}/execute","post","执行紧急访问",["business_date_id","record_id"],_obj()),
        ("/night-audit/break-glass/{record_id}/review","post","复核紧急访问记录",["record_id"],_obj(["decision","reason"],{"decision":{"type":"string"},"reason":{"type":"string"}})),
    ]
    for p,m,s,pp,b in na:
        add(p,m,s,"夜审与紧急访问",path_params=pp,
            query=[("date","string",False,"业务日期 YYYY-MM-DD")] if p=="/night-audit/preview" else [],
            body=b,permission="对应夜审/营业日重开/break-glass 专项权限")
    
    # Guest, inventory, rates and finance read/write.
    items = [
        ("/guests","get","住客列表","住客中心",[],[("q","string",False,"姓名/联系方式搜索")],None,"hotelapp.view_client"),
        ("/guests/{guest_id}","get","住客详情","住客中心",["guest_id"],[],None,"hotelapp.view_client"),
        ("/guests/create","post","创建住客档案","住客中心",[],[],_obj(["full_name"],{"full_name":{"type":"string"},"phone":{"type":"string"},"email":{"type":"string","format":"email"},"notes":{"type":"string"}}),"hotelapp.add_client"),
        ("/guests/{guest_id}/update","post","更新住客档案","住客中心",["guest_id"],[],_obj(properties={"full_name":{"type":"string"},"phone":{"type":"string"},"email":{"type":"string","format":"email"},"notes":{"type":"string"}}),"hotelapp.change_client"),
        ("/guests/{guest_id}/documents/{document_id}/reveal","post","揭示住客证件信息","敏感数据",["guest_id","document_id"],[],_obj(["reason"],{"reason":{"type":"string"}}),"住客证件揭示专项权限 + 原因 + 审计"),
        ("/guests/{guest_id}/phones/{phone_id}/reveal","post","揭示住客电话","敏感数据",["guest_id","phone_id"],[],_obj(["reason"],{"reason":{"type":"string"}}),"住客电话揭示专项权限 + 原因 + 审计"),
        ("/rooms/status","get","房态列表","房态库存",[],[("floor","string",False,"楼层"),("housekeeping","string",False,"客房清洁状态"),("room_type_id","integer",False,"房型 ID"),("q","string",False,"房号搜索")],None,"hotelapp.view_room"),
        ("/room-types","get","房型列表","房态库存",[],[],None,"hotelapp.view_room"),
        ("/rate-calendar","get","房价日历","房价管理",[],[("start","string",False,"开始日期"),("days","integer",False,"查询天数，最多 30"),("room_type_id","integer",False,"房型 ID")],None,"hotelapp.view_room"),
        ("/financial-summary","get","财务汇总","财务",[],[],None,"财务查看权限"),
        ("/daily-rates/{rate_id}/adjust","post","调整每日房价","房价管理",["rate_id"],[],_obj(["price","reason"],{"price":{"type":"string"},"reason":{"type":"string"}}),"hotelapp.change_room"),
        ("/approvals","get","审批记录列表","审批",[],[("status","string",False,"审批状态")],None,"审批查看权限"),
        ("/approvals/request","post","创建审批申请","审批",[],[],_obj(["action_code","entity_type","entity_id","reason"],{"action_code":{"type":"string"},"entity_type":{"type":"string"},"entity_id":{"type":"string"},"reason":{"type":"string"},"payload":{"type":"object"}}),"业务审批申请权限"),
        ("/approvals/{record_id}/approve","post","批准审批申请","审批",["record_id"],[],_obj(),"审批人权限；申请人与审批人分离"),
        ("/approvals/{record_id}/reject","post","驳回审批申请","审批",["record_id"],[],_obj(["reason"],{"reason":{"type":"string"}}),"审批人权限；申请人与审批人分离"),
        ("/reservations-mgmt","get","PMS 预订列表","预订管理",[],[("status","string",False,"预订状态"),("q","string",False,"预订号/住客搜索")],None,"预订查看权限"),
        ("/reservations-mgmt/{booking_no}","get","PMS 预订详情","预订管理",["booking_no"],[],None,"预订查看权限"),
        ("/audit-timeline","get","审计时间线","审计",[],[("entity_type","string",False,"实体类型"),("entity_id","string",False,"实体 ID"),("action","string",False,"动作")],None,"审计查看权限"),
        ("/staff","get","员工列表","员工中心",[],[("q","string",False,"姓名/工号搜索")],None,"identity.staff_manage"),
        ("/staff/{profile_id}","get","员工详情","员工中心",["profile_id"],[],None,"identity.staff_manage"),
        ("/memberships","get","会员列表","会员中心",[],[("status","string",False,"会员状态"),("q","string",False,"会员号/住客搜索")],None,"hotelapp.view_vip"),
        ("/memberships/{membership_id}","get","会员详情","会员中心",["membership_id"],[],None,"hotelapp.view_vip"),
        ("/memberships/create","post","创建会员关系","会员中心",[],[],_obj(["guest_id"],{"guest_id":{"type":"integer"},"member_no":{"type":"string"},"level_code":{"type":"string"},"joined_at":{"type":"string","format":"date"}}),"hotelapp.add_vip"),
        ("/memberships/{membership_id}/level","post","变更会员等级","会员中心",["membership_id"],[],_obj(["level_code","reason"],{"level_code":{"type":"string"},"reason":{"type":"string"}}),"hotelapp.change_vip"),
    ]
    for p,m,s,t,pp,qq,b,perm in items:
        add(p,m,s,t,path_params=pp,query=qq,body=b,permission=perm,
            success_code="201" if m=="post" and p in ("/guests/create","/memberships/create","/approvals/request") else "200")
    add("/openapi.json","get","获取 OpenAPI 规范","文档",auth=False,success="OpenAPI 3.1 JSON 文档")
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "PMS2 酒店管理系统 API",
            "version": "2.1.0",
            "description": "PMS2 模块化单体 API 契约。只包含当前注册的 JSON API；HTML 页面、表单动作和历史页面入口不作为 REST API。\n\n重要：x-contract-status=implemented-route-field-contract-partial 表示路由已实现，但完整 DTO/错误字段仍需继续核验。登录目前是 Django Session HTML Form，不存在 JSON 登录接口。",
        },
        "servers": [{"url": "/"}],
        "tags": [{"name": x} for x in ["公开预订","支付","前台接待","夜审与紧急访问","住客中心","敏感数据","房态库存","房价管理","财务","审批","预订管理","审计","员工中心","会员中心","文档"]],
        "paths": paths,
        "components": {
            "securitySchemes": {"PMS2Session": {"type":"apiKey","in":"cookie","name":"sessionid","description":"Django Session 登录 Cookie。员工登录入口为 POST /login/ HTML Form，不是 JSON API。"}},
            "schemas": {
                "ApiSuccessEnvelope": {"type":"object","required":["data","meta"],"properties":{"data":{"type":"object"},"meta":{"type":"object","properties":{"request_id":{"type":"string"},"source_system":{"type":"string","example":"PMS2"},"generated_at":{"type":"string","format":"date-time"}}}}},
                "ApiErrorEnvelope": {"type":"object","required":["error","meta"],"properties":{"error":{"type":"object","properties":{"code":{"type":"string"},"message":{"type":"string"},"field_errors":{"type":"object"},"retryable":{"type":"boolean"}}},"meta":{"type":"object"}}},
                "PublicApiError": {"type":"object","properties":{"error":{"type":"string"},"retry_after":{"type":"integer"}}}
            }
        }
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
