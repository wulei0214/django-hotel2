"""ARCH-04 H2：旧 ContentType 权限码的归属台账（生成件，勿手工改）。

背景：C10 把员工模块解绑到新栈权限码后，`auth.Permission` 里仍留着一批绑定在
旧 ContentType（`hotelapp.*`）上的权限码。其中一部分已经没有任何门禁引用——
既不在模块注册表里，也不在任何在线路由的权限判定里。这就是「孤儿权限码」：
权限码存在，但没有归属（没人知道它管什么、退役后会不会被误授）。

本台账给**每一个** `hotelapp.*` 权限码一个明确归属，使「无归属权限码」不再存在。

归属分类
--------
* `MODULE_GATE` —— 仍被 `MODULE_REGISTRY_V2` 某模块引用（模块门禁或专项码）；
* `LEGACY_ROUTE_GATE` —— 只被旧页面/旧路由引用，注册表已不再引用；
* `DORMANT` —— 注册表与生产代码都不引用。

「历史码」（`historical=True`）
---------------------------
其所属模型已不在 `hotelapp`（模型已迁往 `apps/billing` 等新域）。这类权限行
只在「从更早状态迁移过来的库」里存在（ContentType 是历史迁移留下的），
**全新迁移的库不会生成它们**。本表仍保留登记（避免出现无归属），但门禁不要求
它们在当前库里存在——否则「全新迁移库」与「老库升级库」的结论会不一致。

关于「不删除 DORMANT 码」
--------------------------
Django 的 `post_migrate` 会为每个模型重建 `add/change/delete/view` 默认权限，
删了下次 `migrate` 又会回来；删除只会在两次 migrate 之间制造「曾经不存在」的
假象，且历史角色的授权记录会指向不存在的权限。**因此治理方式是把它们登记为
`DORMANT`（明确「不使用」），而不是删库行**——这既消除了「无归属」状态，又不
破坏可追溯性。任何把 DORMANT 码重新接入门禁的行为都会被门禁用例拦下。

生成依据：`apps/identity/registry.py` + 全仓生产代码字面量扫描 +
`auth.Permission(content_type__app_label='hotelapp')` 实测清单。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class LegacyPermissionDisposition:
    """单个旧权限码的归属记录。"""

    code: str
    disposition: str
    owner: str
    note: str
    mapped_to: str = ""
    historical: bool = False


MODULE_GATE = "MODULE_GATE"
LEGACY_ROUTE_GATE = "LEGACY_ROUTE_GATE"
DORMANT = "DORMANT"


LEGACY_PERMISSION_CODE_DISPOSITIONS = {
    "hotelapp.add_accommodation": LegacyPermissionDisposition(
        code="hotelapp.add_accommodation",
        disposition="MODULE_GATE",
        owner="accommodation",
        note="模块注册表门禁/专项码，仍在用",
    ),
    "hotelapp.change_accommodation": LegacyPermissionDisposition(
        code="hotelapp.change_accommodation",
        disposition="MODULE_GATE",
        owner="accommodation",
        note="模块注册表门禁/专项码，仍在用",
    ),
    "hotelapp.delete_accommodation": LegacyPermissionDisposition(
        code="hotelapp.delete_accommodation",
        disposition="MODULE_GATE",
        owner="accommodation",
        note="模块注册表门禁/专项码，仍在用",
    ),
    "hotelapp.no_show_handle": LegacyPermissionDisposition(
        code="hotelapp.no_show_handle",
        disposition="MODULE_GATE",
        owner="accommodation",
        note="模块注册表门禁/专项码，仍在用",
    ),
    "hotelapp.reverse_checkout": LegacyPermissionDisposition(
        code="hotelapp.reverse_checkout",
        disposition="DORMANT",
        owner="旧模型默认权限（Django 自动生成）",
        note="F-08 悬空码：唯一检查点随 /uncheckout/、/room_status/ 410 退役，PMS2 冲销改走追加式账本，旧码不再作为执行授权（保留不删，见下）",
    ),
    "hotelapp.view_accommodation": LegacyPermissionDisposition(
        code="hotelapp.view_accommodation",
        disposition="MODULE_GATE",
        owner="accommodation",
        note="模块注册表门禁/专项码，仍在用",
    ),
    "hotelapp.add_auditlog": LegacyPermissionDisposition(
        code="hotelapp.add_auditlog",
        disposition="DORMANT",
        owner="旧模型默认权限（Django 自动生成）",
        note="Django 为旧模型自动生成的默认 CRUD 权限码：任何门禁都不引用",
        historical=True,
    ),
    "hotelapp.change_auditlog": LegacyPermissionDisposition(
        code="hotelapp.change_auditlog",
        disposition="DORMANT",
        owner="旧模型默认权限（Django 自动生成）",
        note="Django 为旧模型自动生成的默认 CRUD 权限码：任何门禁都不引用",
        historical=True,
    ),
    "hotelapp.delete_auditlog": LegacyPermissionDisposition(
        code="hotelapp.delete_auditlog",
        disposition="DORMANT",
        owner="旧模型默认权限（Django 自动生成）",
        note="Django 为旧模型自动生成的默认 CRUD 权限码：任何门禁都不引用",
        historical=True,
    ),
    "hotelapp.view_auditlog": LegacyPermissionDisposition(
        code="hotelapp.view_auditlog",
        disposition="DORMANT",
        owner="旧模型默认权限（Django 自动生成）",
        note="Django 为旧模型自动生成的默认 CRUD 权限码：任何门禁都不引用",
        historical=True,
    ),
    "hotelapp.add_client": LegacyPermissionDisposition(
        code="hotelapp.add_client",
        disposition="MODULE_GATE",
        owner="guests",
        note="模块注册表门禁/专项码，仍在用",
    ),
    "hotelapp.change_client": LegacyPermissionDisposition(
        code="hotelapp.change_client",
        disposition="MODULE_GATE",
        owner="guests",
        note="模块注册表门禁/专项码，仍在用",
    ),
    "hotelapp.delete_client": LegacyPermissionDisposition(
        code="hotelapp.delete_client",
        disposition="MODULE_GATE",
        owner="guests",
        note="模块注册表门禁/专项码，仍在用",
    ),
    "hotelapp.export_pii": LegacyPermissionDisposition(
        code="hotelapp.export_pii",
        disposition="MODULE_GATE",
        owner="staff",
        note="模块注册表门禁/专项码，仍在用",
    ),
    "hotelapp.guest_document_reveal": LegacyPermissionDisposition(
        code="hotelapp.guest_document_reveal",
        disposition="MODULE_GATE",
        owner="guests",
        note="模块注册表门禁/专项码，仍在用",
    ),
    "hotelapp.guest_phone_reveal": LegacyPermissionDisposition(
        code="hotelapp.guest_phone_reveal",
        disposition="MODULE_GATE",
        owner="guests",
        note="模块注册表门禁/专项码，仍在用",
    ),
    "hotelapp.view_client": LegacyPermissionDisposition(
        code="hotelapp.view_client",
        disposition="MODULE_GATE",
        owner="guests",
        note="模块注册表门禁/专项码，仍在用",
    ),
    "hotelapp.add_financerecord": LegacyPermissionDisposition(
        code="hotelapp.add_financerecord",
        disposition="DORMANT",
        owner="旧模型默认权限（Django 自动生成）",
        note="Django 为旧模型自动生成的默认 CRUD 权限码：任何门禁都不引用",
    ),
    "hotelapp.change_financerecord": LegacyPermissionDisposition(
        code="hotelapp.change_financerecord",
        disposition="DORMANT",
        owner="旧模型默认权限（Django 自动生成）",
        note="Django 为旧模型自动生成的默认 CRUD 权限码：任何门禁都不引用",
    ),
    "hotelapp.delete_financerecord": LegacyPermissionDisposition(
        code="hotelapp.delete_financerecord",
        disposition="DORMANT",
        owner="旧模型默认权限（Django 自动生成）",
        note="Django 为旧模型自动生成的默认 CRUD 权限码：任何门禁都不引用",
    ),
    "hotelapp.finance_access": LegacyPermissionDisposition(
        code="hotelapp.finance_access",
        disposition="MODULE_GATE",
        owner="finance",
        note="模块注册表门禁/专项码，仍在用",
    ),
    "hotelapp.view_financerecord": LegacyPermissionDisposition(
        code="hotelapp.view_financerecord",
        disposition="DORMANT",
        owner="旧模型默认权限（Django 自动生成）",
        note="Django 为旧模型自动生成的默认 CRUD 权限码：任何门禁都不引用",
    ),
    "hotelapp.add_folio": LegacyPermissionDisposition(
        code="hotelapp.add_folio",
        disposition="DORMANT",
        owner="旧模型默认权限（Django 自动生成）",
        note="Django 为旧模型自动生成的默认 CRUD 权限码：任何门禁都不引用",
    ),
    "hotelapp.change_folio": LegacyPermissionDisposition(
        code="hotelapp.change_folio",
        disposition="DORMANT",
        owner="旧模型默认权限（Django 自动生成）",
        note="Django 为旧模型自动生成的默认 CRUD 权限码：任何门禁都不引用",
    ),
    "hotelapp.delete_folio": LegacyPermissionDisposition(
        code="hotelapp.delete_folio",
        disposition="DORMANT",
        owner="旧模型默认权限（Django 自动生成）",
        note="Django 为旧模型自动生成的默认 CRUD 权限码：任何门禁都不引用",
    ),
    "hotelapp.view_folio": LegacyPermissionDisposition(
        code="hotelapp.view_folio",
        disposition="DORMANT",
        owner="旧模型默认权限（Django 自动生成）",
        note="Django 为旧模型自动生成的默认 CRUD 权限码：任何门禁都不引用",
    ),
    "hotelapp.add_food": LegacyPermissionDisposition(
        code="hotelapp.add_food",
        disposition="MODULE_GATE",
        owner="fnb",
        note="模块注册表门禁/专项码，仍在用",
    ),
    "hotelapp.change_food": LegacyPermissionDisposition(
        code="hotelapp.change_food",
        disposition="MODULE_GATE",
        owner="fnb",
        note="模块注册表门禁/专项码，仍在用",
    ),
    "hotelapp.delete_food": LegacyPermissionDisposition(
        code="hotelapp.delete_food",
        disposition="MODULE_GATE",
        owner="fnb",
        note="模块注册表门禁/专项码，仍在用",
    ),
    "hotelapp.view_food": LegacyPermissionDisposition(
        code="hotelapp.view_food",
        disposition="MODULE_GATE",
        owner="fnb",
        note="模块注册表门禁/专项码，仍在用",
    ),
    "hotelapp.add_ledgerentry": LegacyPermissionDisposition(
        code="hotelapp.add_ledgerentry",
        disposition="DORMANT",
        owner="旧模型默认权限（Django 自动生成）",
        note="Django 为旧模型自动生成的默认 CRUD 权限码：任何门禁都不引用",
        historical=True,
    ),
    "hotelapp.change_ledgerentry": LegacyPermissionDisposition(
        code="hotelapp.change_ledgerentry",
        disposition="DORMANT",
        owner="旧模型默认权限（Django 自动生成）",
        note="Django 为旧模型自动生成的默认 CRUD 权限码：任何门禁都不引用",
        historical=True,
    ),
    "hotelapp.delete_ledgerentry": LegacyPermissionDisposition(
        code="hotelapp.delete_ledgerentry",
        disposition="DORMANT",
        owner="旧模型默认权限（Django 自动生成）",
        note="Django 为旧模型自动生成的默认 CRUD 权限码：任何门禁都不引用",
        historical=True,
    ),
    "hotelapp.view_ledgerentry": LegacyPermissionDisposition(
        code="hotelapp.view_ledgerentry",
        disposition="DORMANT",
        owner="旧模型默认权限（Django 自动生成）",
        note="Django 为旧模型自动生成的默认 CRUD 权限码：任何门禁都不引用",
        historical=True,
    ),
    "hotelapp.add_nightaudit": LegacyPermissionDisposition(
        code="hotelapp.add_nightaudit",
        disposition="DORMANT",
        owner="旧模型默认权限（Django 自动生成）",
        note="Django 为旧模型自动生成的默认 CRUD 权限码：任何门禁都不引用",
    ),
    "hotelapp.business_date_reopen": LegacyPermissionDisposition(
        code="hotelapp.business_date_reopen",
        disposition="MODULE_GATE",
        owner="night_audit",
        note="模块注册表门禁/专项码，仍在用",
    ),
    "hotelapp.change_nightaudit": LegacyPermissionDisposition(
        code="hotelapp.change_nightaudit",
        disposition="DORMANT",
        owner="旧模型默认权限（Django 自动生成）",
        note="Django 为旧模型自动生成的默认 CRUD 权限码：任何门禁都不引用",
    ),
    "hotelapp.delete_nightaudit": LegacyPermissionDisposition(
        code="hotelapp.delete_nightaudit",
        disposition="DORMANT",
        owner="旧模型默认权限（Django 自动生成）",
        note="Django 为旧模型自动生成的默认 CRUD 权限码：任何门禁都不引用",
    ),
    "hotelapp.night_audit_execute": LegacyPermissionDisposition(
        code="hotelapp.night_audit_execute",
        disposition="MODULE_GATE",
        owner="night_audit",
        note="模块注册表门禁/专项码，仍在用",
    ),
    "hotelapp.view_nightaudit": LegacyPermissionDisposition(
        code="hotelapp.view_nightaudit",
        disposition="DORMANT",
        owner="旧模型默认权限（Django 自动生成）",
        note="Django 为旧模型自动生成的默认 CRUD 权限码：任何门禁都不引用",
    ),
    "hotelapp.add_order": LegacyPermissionDisposition(
        code="hotelapp.add_order",
        disposition="MODULE_GATE",
        owner="fnb",
        note="模块注册表门禁/专项码，仍在用",
    ),
    "hotelapp.change_order": LegacyPermissionDisposition(
        code="hotelapp.change_order",
        disposition="MODULE_GATE",
        owner="fnb",
        note="模块注册表门禁/专项码，仍在用",
    ),
    "hotelapp.delete_order": LegacyPermissionDisposition(
        code="hotelapp.delete_order",
        disposition="MODULE_GATE",
        owner="fnb",
        note="模块注册表门禁/专项码，仍在用",
    ),
    "hotelapp.view_order": LegacyPermissionDisposition(
        code="hotelapp.view_order",
        disposition="MODULE_GATE",
        owner="fnb",
        note="模块注册表门禁/专项码，仍在用",
    ),
    "hotelapp.add_park": LegacyPermissionDisposition(
        code="hotelapp.add_park",
        disposition="MODULE_GATE",
        owner="parking",
        note="模块注册表门禁/专项码，仍在用",
    ),
    "hotelapp.change_park": LegacyPermissionDisposition(
        code="hotelapp.change_park",
        disposition="MODULE_GATE",
        owner="parking",
        note="模块注册表门禁/专项码，仍在用",
    ),
    "hotelapp.delete_park": LegacyPermissionDisposition(
        code="hotelapp.delete_park",
        disposition="MODULE_GATE",
        owner="parking",
        note="模块注册表门禁/专项码，仍在用",
    ),
    "hotelapp.view_park": LegacyPermissionDisposition(
        code="hotelapp.view_park",
        disposition="MODULE_GATE",
        owner="parking",
        note="模块注册表门禁/专项码，仍在用",
    ),
    "hotelapp.add_payment": LegacyPermissionDisposition(
        code="hotelapp.add_payment",
        disposition="DORMANT",
        owner="旧模型默认权限（Django 自动生成）",
        note="Django 为旧模型自动生成的默认 CRUD 权限码：任何门禁都不引用",
        historical=True,
    ),
    "hotelapp.change_payment": LegacyPermissionDisposition(
        code="hotelapp.change_payment",
        disposition="DORMANT",
        owner="旧模型默认权限（Django 自动生成）",
        note="Django 为旧模型自动生成的默认 CRUD 权限码：任何门禁都不引用",
        historical=True,
    ),
    "hotelapp.delete_payment": LegacyPermissionDisposition(
        code="hotelapp.delete_payment",
        disposition="DORMANT",
        owner="旧模型默认权限（Django 自动生成）",
        note="Django 为旧模型自动生成的默认 CRUD 权限码：任何门禁都不引用",
        historical=True,
    ),
    "hotelapp.view_payment": LegacyPermissionDisposition(
        code="hotelapp.view_payment",
        disposition="DORMANT",
        owner="旧模型默认权限（Django 自动生成）",
        note="Django 为旧模型自动生成的默认 CRUD 权限码：任何门禁都不引用",
        historical=True,
    ),
    "hotelapp.add_reservation": LegacyPermissionDisposition(
        code="hotelapp.add_reservation",
        disposition="DORMANT",
        owner="旧模型默认权限（Django 自动生成）",
        note="Django 为旧模型自动生成的默认 CRUD 权限码：任何门禁都不引用",
        historical=True,
    ),
    "hotelapp.change_reservation": LegacyPermissionDisposition(
        code="hotelapp.change_reservation",
        disposition="DORMANT",
        owner="旧模型默认权限（Django 自动生成）",
        note="Django 为旧模型自动生成的默认 CRUD 权限码：任何门禁都不引用",
        historical=True,
    ),
    "hotelapp.delete_reservation": LegacyPermissionDisposition(
        code="hotelapp.delete_reservation",
        disposition="DORMANT",
        owner="旧模型默认权限（Django 自动生成）",
        note="Django 为旧模型自动生成的默认 CRUD 权限码：任何门禁都不引用",
        historical=True,
    ),
    "hotelapp.view_reservation": LegacyPermissionDisposition(
        code="hotelapp.view_reservation",
        disposition="DORMANT",
        owner="旧模型默认权限（Django 自动生成）",
        note="Django 为旧模型自动生成的默认 CRUD 权限码：任何门禁都不引用",
        historical=True,
    ),
    "hotelapp.add_room": LegacyPermissionDisposition(
        code="hotelapp.add_room",
        disposition="MODULE_GATE",
        owner="rooms",
        note="模块注册表门禁/专项码，仍在用",
    ),
    "hotelapp.change_room": LegacyPermissionDisposition(
        code="hotelapp.change_room",
        disposition="MODULE_GATE",
        owner="rooms",
        note="模块注册表门禁/专项码，仍在用",
    ),
    "hotelapp.delete_room": LegacyPermissionDisposition(
        code="hotelapp.delete_room",
        disposition="MODULE_GATE",
        owner="rooms",
        note="模块注册表门禁/专项码，仍在用",
    ),
    "hotelapp.room_release_ready": LegacyPermissionDisposition(
        code="hotelapp.room_release_ready",
        disposition="MODULE_GATE",
        owner="rooms",
        note="模块注册表门禁/专项码，仍在用",
    ),
    "hotelapp.view_room": LegacyPermissionDisposition(
        code="hotelapp.view_room",
        disposition="MODULE_GATE",
        owner="rooms",
        note="模块注册表门禁/专项码，仍在用",
    ),
    "hotelapp.add_roomtypeinventory": LegacyPermissionDisposition(
        code="hotelapp.add_roomtypeinventory",
        disposition="DORMANT",
        owner="旧模型默认权限（Django 自动生成）",
        note="Django 为旧模型自动生成的默认 CRUD 权限码：任何门禁都不引用",
        historical=True,
    ),
    "hotelapp.change_roomtypeinventory": LegacyPermissionDisposition(
        code="hotelapp.change_roomtypeinventory",
        disposition="DORMANT",
        owner="旧模型默认权限（Django 自动生成）",
        note="Django 为旧模型自动生成的默认 CRUD 权限码：任何门禁都不引用",
        historical=True,
    ),
    "hotelapp.delete_roomtypeinventory": LegacyPermissionDisposition(
        code="hotelapp.delete_roomtypeinventory",
        disposition="DORMANT",
        owner="旧模型默认权限（Django 自动生成）",
        note="Django 为旧模型自动生成的默认 CRUD 权限码：任何门禁都不引用",
        historical=True,
    ),
    "hotelapp.view_roomtypeinventory": LegacyPermissionDisposition(
        code="hotelapp.view_roomtypeinventory",
        disposition="DORMANT",
        owner="旧模型默认权限（Django 自动生成）",
        note="Django 为旧模型自动生成的默认 CRUD 权限码：任何门禁都不引用",
        historical=True,
    ),
    "hotelapp.add_staff": LegacyPermissionDisposition(
        code="hotelapp.add_staff",
        disposition="LEGACY_ROUTE_GATE",
        owner="hotelapp 旧页面/旧路由",
        note="旧路由门禁仍按旧码判定（重定向时保留等价性）",
        mapped_to="identity.add_staffprofile",
    ),
    "hotelapp.change_staff": LegacyPermissionDisposition(
        code="hotelapp.change_staff",
        disposition="LEGACY_ROUTE_GATE",
        owner="hotelapp 旧页面/旧路由",
        note="旧路由门禁仍按旧码判定（重定向时保留等价性）",
        mapped_to="identity.change_staffprofile",
    ),
    "hotelapp.delete_staff": LegacyPermissionDisposition(
        code="hotelapp.delete_staff",
        disposition="LEGACY_ROUTE_GATE",
        owner="hotelapp 旧页面/旧路由",
        note="旧路由门禁仍按旧码判定（重定向时保留等价性）",
        mapped_to="identity.delete_staffprofile",
    ),
    "hotelapp.export_approve": LegacyPermissionDisposition(
        code="hotelapp.export_approve",
        disposition="MODULE_GATE",
        owner="staff",
        note="模块注册表门禁/专项码，仍在用",
    ),
    "hotelapp.view_staff": LegacyPermissionDisposition(
        code="hotelapp.view_staff",
        disposition="LEGACY_ROUTE_GATE",
        owner="hotelapp 旧页面/旧路由",
        note="旧路由门禁仍按旧码判定（重定向时保留等价性）",
        mapped_to="identity.view_staffprofile",
    ),
    "hotelapp.add_vip": LegacyPermissionDisposition(
        code="hotelapp.add_vip",
        disposition="MODULE_GATE",
        owner="memberships",
        note="模块注册表门禁/专项码，仍在用",
    ),
    "hotelapp.change_vip": LegacyPermissionDisposition(
        code="hotelapp.change_vip",
        disposition="MODULE_GATE",
        owner="memberships",
        note="模块注册表门禁/专项码，仍在用",
    ),
    "hotelapp.delete_vip": LegacyPermissionDisposition(
        code="hotelapp.delete_vip",
        disposition="MODULE_GATE",
        owner="memberships",
        note="模块注册表门禁/专项码，仍在用",
    ),
    "hotelapp.view_vip": LegacyPermissionDisposition(
        code="hotelapp.view_vip",
        disposition="MODULE_GATE",
        owner="memberships",
        note="模块注册表门禁/专项码，仍在用",
    ),
}


def disposition_for(code):
    """取某旧权限码的归属记录；未登记返回 None（= 无归属，治理失败）。"""
    return LEGACY_PERMISSION_CODE_DISPOSITIONS.get(code)
