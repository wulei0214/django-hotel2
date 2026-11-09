"""Path-isolated authentication cookies for PMS, platform portal and Django Admin.

平台与业务 Session Cookie 网络隔离专项（方案 A，设计基线 §B）：
平台 Session 服务端只有一份记录，两个 Path Cookie 是同一 Session Key 的
传输别名，不是两份独立会话：
  * platform_sessionid @ /platform/    平台门户
  * django_admin_sessionid @ /admin/   Django Admin
  * admin_sessionid（历史 Path 组合）仅发送过期删除指令，不再认证读取
  * PMS sessionid @ / 保持 Django 原生行为

路径分类、Cookie 名称与属性全部由本模块的规格表（SessionCookieSpec）
单一权威派生；读取、写入、刷新、删除与测试共享同一规格，禁止在多个
函数里散落硬编码名称与 Path（§B.1）。

并发语义采用评审决议 H.2 的务实一致：不承诺旧响应绝不写回旧 Key，
但已失效的旧 Key 不能重新获得认证（Session 引擎按无效处理）。
"""
import time

from django.conf import settings
from django.contrib.sessions.backends.base import UpdateError
from django.contrib.sessions.exceptions import SessionInterrupted
from django.contrib.sessions.middleware import SessionMiddleware
from django.utils.cache import patch_vary_headers
from django.utils.http import http_date
from http.cookies import Morsel


# ---- 身份域（identity）：Cookie 归属的认证体系 ----
COOKIE_DOMAIN_PMS = "pms"
COOKIE_DOMAIN_PLATFORM = "platform"
COOKIE_DOMAIN_LEGACY = "legacy-platform"  # 历史平台 Cookie：仅迁移清理

# ---- 路径分类（读取规格 category，§B.2 步骤 1）----
CATEGORY_PMS = "pms"
CATEGORY_PLATFORM_PORTAL = "platform-portal"
CATEGORY_PLATFORM_ADMIN = "platform-admin"
CATEGORY_LEGACY_REDIRECT = "legacy-redirect"

# 过期删除指令的通用属性（RFC 6265：Max-Age=0 + 已过期的 Expires）
_DELETE_MAX_AGE = 0
_DELETE_EXPIRES = "Thu, 01 Jan 1970 00:00:00 GMT"


class SessionCookieSpec:
    """单个 Session Cookie 的唯一权威规格（§B.1 规格表的一行）。

    identity：Cookie 归属的认证体系（PMS / 平台 / 历史平台）。
    name/path/domain/secure/httponly/samesite：浏览器侧传输属性；
    domain 为 None 表示 host-only（不设置 Domain 属性）。
    writable：平台 Session 保存/轮换时是否写入。
    cleanup_only：True 表示仅发送过期删除指令，绝不作为认证读取。
    """

    __slots__ = ("category", "identity", "name", "path", "paths", "domain",
                 "secure", "httponly", "samesite", "writable", "cleanup_only")

    def __init__(self, category, identity, name, path=None, paths=None,
                 domain=None, secure=False, httponly=True, samesite=None,
                 writable=True, cleanup_only=False):
        self.category = category
        self.identity = identity
        self.name = name
        self.path = path
        self.paths = paths if paths is not None else ((path,) if path else ())
        self.domain = domain
        self.secure = bool(secure)
        self.httponly = bool(httponly)
        self.samesite = samesite
        self.writable = bool(writable)
        self.cleanup_only = bool(cleanup_only)

    def __repr__(self):
        return "<SessionCookieSpec %s name=%r path=%r>" % (
            self.category, self.name, self.path)


def _global_cookie_flags():
    """平台 Cookie 安全属性沿用当前全局配置（不可静默改变，§4.2）。"""
    return {
        "secure": settings.SESSION_COOKIE_SECURE,
        "httponly": settings.SESSION_COOKIE_HTTPONLY,
        "samesite": settings.SESSION_COOKIE_SAMESITE,
    }


def pms_session_cookie_spec():
    """PMS 会话 Cookie：名称/Path/Domain 等沿用现有 Django 配置，行为不变。"""
    return SessionCookieSpec(
        category=CATEGORY_PMS,
        identity=COOKIE_DOMAIN_PMS,
        name=settings.SESSION_COOKIE_NAME,
        path=settings.SESSION_COOKIE_PATH,
        domain=settings.SESSION_COOKIE_DOMAIN,
        secure=settings.SESSION_COOKIE_SECURE,
        httponly=settings.SESSION_COOKIE_HTTPONLY,
        samesite=settings.SESSION_COOKIE_SAMESITE,
        writable=True,
    )


def platform_portal_cookie_spec():
    """平台门户 Cookie：Path=/platform/，host-only（§4.1 不设 Domain）。"""
    return SessionCookieSpec(
        category=CATEGORY_PLATFORM_PORTAL,
        identity=COOKIE_DOMAIN_PLATFORM,
        name=getattr(settings, "PLATFORM_PORTAL_COOKIE_NAME",
                     "platform_sessionid"),
        path="/platform/",
        domain=None,
        **_global_cookie_flags()
    )


def platform_admin_cookie_spec():
    """Django Admin Cookie：Path=/admin/，host-only；与门户同一平台 Session。"""
    return SessionCookieSpec(
        category=CATEGORY_PLATFORM_ADMIN,
        identity=COOKIE_DOMAIN_PLATFORM,
        name=getattr(settings, "ADMIN_SESSION_COOKIE_NAME",
                     "django_admin_sessionid"),
        path="/admin/",
        domain=None,
        **_global_cookie_flags()
    )


def legacy_platform_cookie_spec():
    """历史平台 Cookie（admin_sessionid）：仅迁移清理，不再认证读取（§D）。"""
    return SessionCookieSpec(
        category=CATEGORY_LEGACY_REDIRECT,
        identity=COOKIE_DOMAIN_LEGACY,
        name=getattr(settings, "LEGACY_PLATFORM_COOKIE_NAME",
                     "admin_sessionid"),
        paths=tuple(getattr(settings, "LEGACY_PLATFORM_COOKIE_PATHS",
                            ("/", "/admin/", "/platform/",
                             "/permissions/", "/permissions/save/"))),
        # Domain 必须与历史设置一致：历史 Cookie 由旧中间件按
        # SESSION_COOKIE_DOMAIN 写出（默认 host-only），不猜测父域（§D.2）。
        domain=settings.SESSION_COOKIE_DOMAIN,
        secure=settings.SESSION_COOKIE_SECURE,
        httponly=settings.SESSION_COOKIE_HTTPONLY,
        samesite=settings.SESSION_COOKIE_SAMESITE,
        writable=False,
        cleanup_only=True,
    )


def resolve_session_cookie_spec(path):
    """按规范化路径返回唯一的读取规格（§B.3 契约函数）。

    边界匹配：/platform-foo、/administrator 不算平台路径（§F.1）。
    /permissions* 返回兼容跳转分类（cleanup_only），不加载任何平台
    Session（§E）。未知路径默认 PMS——"未知即平台"禁止（§F.1）。
    """
    normalized = path or "/"
    if normalized == "/platform" or normalized.startswith("/platform/"):
        return platform_portal_cookie_spec()
    if normalized == "/admin" or normalized.startswith("/admin/"):
        return platform_admin_cookie_spec()
    if normalized == "/permissions" or normalized.startswith("/permissions/"):
        return legacy_platform_cookie_spec()
    return pms_session_cookie_spec()


def is_admin_request(request):
    """Return True for platform-management routes.

    Includes Django Admin (with its login/logout URLs), the platform portal
    and the legacy /permissions/ alias (§6.1): all are superuser platform
    surfaces. Kept in sync with resolve_session_cookie_spec; used by
    PropertyContextMiddleware (F-10) to skip PMS property-context parsing.
    """
    path = request.path_info or request.path
    return (path == "/admin" or path.startswith("/admin/")
            or path == "/permissions" or path.startswith("/permissions/")
            or path == "/platform" or path.startswith("/platform/"))


class SplitSessionMiddleware(SessionMiddleware):
    """按路径分类选择会话 Cookie 的 Session Middleware（§B.2 七步契约）。

    1. process_request 依 path_info 分类并只读当前分类允许的 Cookie 名称；
    2. 使用现有 Django Session Engine 加载（不自行解析 Session 数据）；
    3. request.session 绑定到请求，保持 Authentication/CSRF Middleware
       的既有依赖顺序；
    4. 响应阶段按当前身份域写入/刷新/删除 Cookie；
    5. 平台域写入时对两条平台规格执行多 Cookie 写入，值必须相同；
    6. 删除按规格逐项显式传入 Path/Domain/Secure/SameSite，不只删当前路径；
    7. PMS 响应不写/不删平台 Cookie，平台响应不写/不删 PMS Cookie
       （历史平台 Cookie 迁移清理除外）。
    """

    def process_request(self, request):
        spec = resolve_session_cookie_spec(request.path_info or request.path)
        request._session_cookie_spec = spec
        if spec.cleanup_only:
            # 兼容跳转路径（§E）：不读取任何平台 Cookie；绑定未触碰的空
            # Session，仅为满足 Authentication/CSRF Middleware 的依赖。
            request.session = self.SessionStore()
            return
        request.session = self.SessionStore(request.COOKIES.get(spec.name))

    def process_response(self, request, response):
        try:
            accessed = request.session.accessed
            modified = request.session.modified
            empty = request.session.is_empty()
        except AttributeError:
            return response

        spec = getattr(request, "_session_cookie_spec", None)
        if spec is None:
            spec = resolve_session_cookie_spec(request.path_info or request.path)
            request._session_cookie_spec = spec

        if spec.cleanup_only:
            # 兼容跳转（§E）：不发 Cookie、不清 Cookie、不写 Session。
            return response

        if spec.identity == COOKIE_DOMAIN_PLATFORM:
            self._handle_platform_response(
                request, response, spec, accessed, modified, empty)
            return response
        return self._handle_pms_response(
            request, response, spec, accessed, modified, empty)

    # ---- PMS 域：保持 Django SessionMiddleware 原语义 ----

    def _handle_pms_response(self, request, response, spec,
                             accessed, modified, empty):
        if spec.name in request.COOKIES and empty:
            response.delete_cookie(
                spec.name,
                path=spec.path,
                domain=spec.domain,
                samesite=spec.samesite,
            )
            patch_vary_headers(response, ("Cookie",))
            return response
        if accessed:
            patch_vary_headers(response, ("Cookie",))
        if (modified or settings.SESSION_SAVE_EVERY_REQUEST) and not empty:
            if request.session.get_expire_at_browser_close():
                max_age = None
                expires = None
            else:
                max_age = request.session.get_expiry_age()
                expires = http_date(time.time() + max_age)
            # Match Django's SessionMiddleware: never persist a session
            # from an HTTP 500 response.
            if response.status_code != 500:
                try:
                    request.session.save()
                except UpdateError as exc:
                    raise SessionInterrupted(
                        "The request's session was deleted before the "
                        "request completed. The user may have logged out "
                        "in a concurrent request, for example."
                    ) from exc
                self._set_cookie(
                    response, spec, request.session.session_key,
                    max_age, expires)
        return response

    # ---- 平台域：双 Cookie 同一 Session Key（§5、§B.2 步骤 5-7）----

    def _handle_platform_response(self, request, response, spec,
                                  accessed, modified, empty):
        if spec.name in request.COOKIES and empty:
            # 登出 flush / 无效或过期 Cookie（S3/S5）：清理两份平台 Cookie。
            # 触发条件是浏览器持有当前路径平台 Cookie——不凭空推断另一份
            # 是否存在，也不在纯匿名响应里写入任何东西（H.1）。
            self.delete_platform_session_cookies(response)
            patch_vary_headers(response, ("Cookie",))
        else:
            if accessed:
                patch_vary_headers(response, ("Cookie",))
            if (modified or settings.SESSION_SAVE_EVERY_REQUEST) and not empty:
                if request.session.get_expire_at_browser_close():
                    max_age = None
                    expires = None
                else:
                    max_age = request.session.get_expiry_age()
                    expires = http_date(time.time() + max_age)
                # 与 Django SessionMiddleware 一致：500 响应不保存半完成
                # Session；UpdateError 保留并发登出保护语义（§B.4）。
                if response.status_code != 500:
                    try:
                        request.session.save()
                    except UpdateError as exc:
                        raise SessionInterrupted(
                            "The request's session was deleted before the "
                            "request completed. The user may have logged out "
                            "in a concurrent request, for example."
                        ) from exc
                    self.set_platform_session_cookies(
                        response, request.session.session_key,
                        max_age, expires)
        # 迁移清理（§D）：平台域每个响应附加历史组合过期删除指令，幂等。
        self.delete_legacy_platform_cookies(response)
        return response

    # ---- 多 Cookie 写入/删除契约函数（§B.3）----

    def set_platform_session_cookies(self, response, session_key,
                                     max_age=None, expires=None):
        """两条平台规格写入相同 session_key 与一致属性（不变量 A.3.5）。"""
        for spec in self._platform_specs():
            self._set_cookie(response, spec, session_key, max_age, expires)

    def delete_platform_session_cookies(self, response):
        """删除两条平台规格 + 全部历史 Name×Path 组合（幂等，§D.3）。"""
        for spec in self._platform_specs():
            self._delete_cookie(response, spec)
        self.delete_legacy_platform_cookies(response)

    def delete_legacy_platform_cookies(self, response):
        """对历史平台 Cookie 的每个 Name×Path 组合发送过期删除指令。"""
        spec = legacy_platform_cookie_spec()
        for path in spec.paths:
            self._delete_cookie(response, spec, path=path,
                                jar_key="%s@%s" % (spec.name, path))

    def _platform_specs(self):
        return (platform_portal_cookie_spec(), platform_admin_cookie_spec())

    # ---- Cookie 写入/删除底层实现 ----

    def _set_cookie(self, response, spec, session_key, max_age, expires):
        response.set_cookie(
            spec.name,
            session_key,
            max_age=max_age,
            expires=expires,
            domain=spec.domain,
            path=spec.path,
            secure=spec.secure or None,
            httponly=spec.httponly or None,
            samesite=spec.samesite,
        )

    def _delete_cookie(self, response, spec, path=None, jar_key=None):
        """发送过期删除指令，显式匹配 Name/Path/Domain（§B.2 步骤 7）。

        用预构建 Morsel 写入 response.cookies：SimpleCookie 以 Cookie 名为
        键，同一历史名称在不同 Path 下的删除指令必须各自独立存放，WSGI 层
        才会逐项输出独立的 Set-Cookie 头（否则同名组合互相覆盖、只剩最后
        一个 Path）。jar_key 仅用于 response.cookies 存储键，不进入报文。
        """
        path = spec.path if path is None else path
        morsel = Morsel()
        morsel.set(spec.name, "", "")
        morsel["path"] = path
        morsel["max-age"] = _DELETE_MAX_AGE
        morsel["expires"] = _DELETE_EXPIRES
        if spec.domain:
            morsel["domain"] = spec.domain
        # 与 Django delete_cookie 相同的 Secure 兜底：SameSite=None 或
        # __Secure-/__Host- 前缀时浏览器要求 Secure，否则忽略删除指令。
        samesite = spec.samesite
        secure = (
            spec.secure
            or spec.name.startswith(("__Secure-", "__Host-"))
            or (samesite and samesite.lower() == "none")
        )
        if secure:
            morsel["secure"] = True
        if spec.httponly:
            morsel["httponly"] = True
        if samesite:
            morsel["samesite"] = samesite
        response.cookies[jar_key or spec.name] = morsel
