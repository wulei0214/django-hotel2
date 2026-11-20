"""Guest 360 联系方式/证件子资源写 API（MC-003）。

所有写操作复用 apps.guests.services 既有 PII 加密/HMAC/查重/审计服务，
API 只做权限、参数与响应映射，不直接写 GuestPhone/GuestDocument。
"""

from datetime import date

from django.core.exceptions import ValidationError
from django.views.decorators.http import require_POST

from apps.core.api_contract import (
    ApiError,
    api_response,
    api_view,
    idempotent,
    parse_json_object,
)
from apps.guests.models import Guest
from apps.guests.services import GuestService, GuestServiceError, register_document
from apps.identity.authorization import AuthorizationService


def _context(request, permission):
    user = request.user
    if not user.is_authenticated or not user.is_active:
        raise ApiError("AUTHENTICATION_REQUIRED", "请先登录员工账号。")
    property_obj = getattr(request, "property", None)
    if property_obj is None:
        raise ApiError("RESOURCE_NOT_FOUND", "物业上下文不可用。")
    if not AuthorizationService.has_permission(user, property_obj, permission):
        raise ApiError("PERMISSION_DENIED", "当前岗位无权维护客人档案。")
    return property_obj, user


def _guest_or_404(property_obj, guest_id, permission):
    guest = Guest.objects.filter(pk=guest_id, property=property_obj).first()
    if guest is None:
        raise ApiError("RESOURCE_NOT_FOUND", "客人档案不存在或不属于当前物业。")
    return guest


def _phone_or_404(guest, phone_id):
    phone = guest.phones.filter(pk=phone_id).first()
    if phone is None:
        raise ApiError("RESOURCE_NOT_FOUND", "联系方式不存在或不属于该客人。")
    return phone


def _document_or_404(guest, document_id):
    document = guest.documents.filter(pk=document_id).first()
    if document is None:
        raise ApiError("RESOURCE_NOT_FOUND", "证件不存在或不属于该客人。")
    return document


def _day(value, field):
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ApiError("VALIDATION_ERROR", f"{field} 必须是 YYYY-MM-DD。") from None


def _map_service_error(exc):
    message = str(exc)
    if isinstance(exc, ValidationError):
        message = "；".join(getattr(exc, "messages", []) or [message])
    if "不能为空" in message or "必须是" in message:
        raise ApiError("VALIDATION_ERROR", message)
    raise ApiError("BUSINESS_CONFLICT", message)


def _phone_dto(phone):
    return {
        "id": phone.pk,
        "phone_type": phone.phone_type,
        "is_primary": phone.is_primary,
        "shared_review_required": phone.shared_review_required,
        "verified": bool(phone.verified_at),
        "verified_at": phone.verified_at.isoformat() if phone.verified_at else None,
        "source": phone.source,
    }


def _document_dto(document):
    return {
        "id": document.pk,
        "doc_type": document.doc_type,
        "issuing_country": document.issuing_country,
        "expiry_date": document.expiry_date.isoformat() if document.expiry_date else None,
        "verified": bool(document.verified_at),
        "is_primary": document.is_primary,
        "retention_until": (
            document.retention_until.isoformat() if document.retention_until else None
        ),
    }


@require_POST
@api_view
@idempotent
def phone_create(request, guest_id):
    property_obj, user = _context(request, "hotelapp.change_client")
    guest = _guest_or_404(property_obj, guest_id, "hotelapp.change_client")
    payload = parse_json_object(request)
    try:
        phone = GuestService.add_phone(
            guest=guest,
            phone=str(payload.get("phone", "") or ""),
            phone_type=str(payload.get("phone_type", "MOBILE") or "MOBILE"),
            is_primary=bool(payload.get("is_primary", False)),
            source=str(payload.get("source", "前台登记") or "前台登记"),
            added_by=user,
            request=request,
        )
    except (GuestServiceError, ValidationError) as exc:
        _map_service_error(exc)
    return api_response(request, _phone_dto(phone), status=201)


@require_POST
@api_view
@idempotent
def phone_set_primary(request, guest_id, phone_id):
    property_obj, user = _context(request, "hotelapp.change_client")
    guest = _guest_or_404(property_obj, guest_id, "hotelapp.change_client")
    _phone_or_404(guest, phone_id)
    try:
        phone, changed = GuestService.set_primary_phone(
            guest=guest, phone_id=phone_id, changed_by=user, request=request
        )
    except (GuestServiceError, ValidationError) as exc:
        _map_service_error(exc)
    return api_response(request, {**_phone_dto(phone), "changed": changed})


@require_POST
@api_view
@idempotent
def phone_verify(request, guest_id, phone_id):
    property_obj, user = _context(request, "hotelapp.change_client")
    guest = _guest_or_404(property_obj, guest_id, "hotelapp.change_client")
    _phone_or_404(guest, phone_id)
    phone = GuestService.verify_phone(
        guest=guest, phone_id=phone_id, verified_by=user, request=request
    )
    return api_response(request, _phone_dto(phone))


@require_POST
@api_view
@idempotent
def document_create(request, guest_id):
    property_obj, user = _context(request, "hotelapp.change_client")
    guest = _guest_or_404(property_obj, guest_id, "hotelapp.change_client")
    payload = parse_json_object(request)
    try:
        document = register_document(
            guest=guest,
            doc_type=str(payload.get("doc_type", "ID_CARD") or "ID_CARD"),
            doc_no=str(payload.get("doc_no", "") or ""),
            verified_by=user if payload.get("verified") else None,
            issuing_country=str(payload.get("issuing_country", "CN") or "CN"),
            expiry_date=_day(payload.get("expiry_date"), "expiry_date"),
            request=request,
        )
    except (ValidationError, ValueError) as exc:
        _map_service_error(exc)
    return api_response(request, _document_dto(document), status=201)


@require_POST
@api_view
@idempotent
def document_update(request, guest_id, document_id):
    property_obj, user = _context(request, "hotelapp.change_client")
    guest = _guest_or_404(property_obj, guest_id, "hotelapp.change_client")
    document = _document_or_404(guest, document_id)
    payload = parse_json_object(request)
    document, changed = GuestService.update_document(
        document=document,
        doc_type=payload.get("doc_type"),
        expiry_date=_day(payload.get("expiry_date"), "expiry_date"),
        issuing_country=payload.get("issuing_country"),
        is_primary=payload.get("is_primary"),
        changed_by=user,
        request=request,
    )
    return api_response(request, {**_document_dto(document), "changed": changed})


@require_POST
@api_view
@idempotent
def document_verify(request, guest_id, document_id):
    property_obj, user = _context(request, "hotelapp.change_client")
    guest = _guest_or_404(property_obj, guest_id, "hotelapp.change_client")
    document = _document_or_404(guest, document_id)
    document = GuestService.verify_document(document=document, verified_by=user, request=request)
    return api_response(request, _document_dto(document))


@require_POST
@api_view
@idempotent
def document_invalidate(request, guest_id, document_id):
    property_obj, user = _context(request, "hotelapp.change_client")
    guest = _guest_or_404(property_obj, guest_id, "hotelapp.change_client")
    document = _document_or_404(guest, document_id)
    document, changed = GuestService.invalidate_document(
        document=document,
        changed_by=user,
        reason=str(parse_json_object(request).get("reason", "") or ""),
        request=request,
    )
    return api_response(request, {**_document_dto(document), "changed": changed})
