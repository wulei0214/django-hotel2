"""Guest 姓名 PII 加密（V1.0 任务书 TASK-03，阶段 1 双写）。

设计要点（任务书 §TASK-03）：
  * 复用既有 PII 基础设施（_pii_key_bytes/_fernet），禁止另起加密框架；
  * 姓名密文：Fernet(PII_KEY, 规范化姓名)；盲索引：HMAC-SHA256(PII_KEY, 规范化)；
  * 规范化口径：strip → 连续空白折叠为单空格（全角/半角空格同源折叠），
    大小写不折叠（中文姓名为主，折叠大小写会混淆英文姓名检索）；
  * 阶段 1 = 双写（明文 full_name 保留，观察期受控回滚）；读路径与明文
    列退役由负责人批准后的阶段 2 迁移完成；
  * 检索语义说明：icontains 模糊检索是前台核验核心功能，本阶段保留在
    明文列上；full_name_hmac 供阶段 2 切换后的「精确匹配」场景使用。
    模糊→精确的检索语义变化属产品决策，不在本阶段擅自切换。

受控揭示（治理拦截，2026-11-23）：原 `reveal_guest_name` 依赖未获批权限码
`hotelapp.guest_name_reveal`，按负责人治理指令整体移除。见文件末尾说明。
"""

import hashlib
import hmac
import re

from .services import _fernet, _pii_key_bytes

# 姓名 PII 密钥版本（阶段 1 统一 v1；轮换时新增版本并迁移）
PII_KEY_VERSION_V1 = "v1"

# 连续空白（含全角空格）折叠
_WHITESPACE_RE = re.compile(r"[\s\u3000]+")


def normalize_guest_name(raw):
    """姓名规范化：strip + 连续空白（含全角空格）折叠为单个半角空格。

    返回 None 表示输入为空（无姓名可加密/索引）。
    """
    name = _WHITESPACE_RE.sub(" ", (raw or "").strip())
    return name or None


def guest_name_hash(normalized):
    """姓名盲索引：HMAC-SHA256(PII_KEY, 规范化姓名)，不可逆、精确匹配用。"""
    return hmac.new(
        _pii_key_bytes(), (normalized or "").encode("utf-8"), hashlib.sha256
    ).hexdigest()


def encrypt_guest_name(normalized):
    """姓名密文：Fernet(PII_KEY, 规范化姓名)（与手机号/证件密文同源）。"""
    return _fernet().encrypt((normalized or "").encode("utf-8")).decode("ascii")


def decrypt_guest_name(ciphertext):
    """姓名密文解密（仅供受控揭示与迁移校验，禁止业务读路径直连）。"""
    from cryptography.fernet import InvalidToken

    try:
        return _fernet().decrypt((ciphertext or "").encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("姓名密文解密失败：密钥不匹配或数据损坏") from exc


def fill_guest_name_pii(guest, raw_name):
    """为 Guest 实例填充姓名三件套（密文/盲索引/密钥版本）。

    规范化后为空（空姓名）→ 三字段清空。不调用 save（由调用方决定落库时机）。
    """
    normalized = normalize_guest_name(raw_name)
    if normalized is None:
        guest.full_name_ciphertext = None
        guest.full_name_hmac = None
        guest.pii_key_version = None
        return None
    guest.full_name_ciphertext = encrypt_guest_name(normalized)
    guest.full_name_hmac = guest_name_hash(normalized)
    guest.pii_key_version = PII_KEY_VERSION_V1
    return normalized


# ── 姓名完整值揭示：暂停开发（治理拦截）──────────────────────────────
# 2026-11-23 负责人治理指令：NS-E-05 未逐项批准前，任何权限码新增不得合入
# main（正式记录 L172/L189）。原 `reveal_guest_name` 及其掩码辅助函数依赖
# 未获批权限码 `hotelapp.guest_name_reveal`，已整体移除，避免留下「引用未
# 注册权限」的半成品。加密/哈希/双写（fill_guest_name_pii）不受影响，照常可用。
#
# 恢复条件（三者缺一不可）：
#   1. 负责人就 `hotelapp.guest_name_reveal` 逐项批准（NS-E-05 六要素）；
#   2. 在 apps/identity/registry.py 的 SPECIAL_PERMISSIONS 完成登记；
#   3. 权限迁移、授权面、测试矩阵与文档同步落地。
# 审核入口：docs/acceptance/权限违规拦截与OWNER裁定执行报告_2026-11-23.md
