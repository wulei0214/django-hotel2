"""P0 真实 HTTP 冒烟套件公共设施（实施方案 §9.11）。

设计要点：
  * 纯 requests 实现真实网络请求，不经过 Django test client，也不加载
    Django settings——与既有 Django 单测（manage.py test）完全隔离。
  * 目标实例由环境变量 PMS_BASE_URL 指定（默认 http://127.0.0.1:8000），
    该实例必须已完成 migrate + 演示数据种子（见 README.md）。
  * CSRF 协议（CSRF_USE_SESSIONS=True）：令牌保存在服务端会话中，
    由页面上的 csrfmiddlewaretoken 隐藏域下发；所有写请求必须携带
    X-CSRFToken 头（与 /reserve/ 页面内联 JS、OpenAPI 契约同口径）。
    HttpApi.ensure_csrf() 负责从公开页获取/刷新令牌；登录成功后
    Django 会轮换 CSRF 令牌，需重新获取。
  * 员工登录走真实 POST /login/ 表单流：GET 登录页解析令牌 → 表单提交
    （不跟随重定向：成功 = 302 跳 /home/，失败 = 200 回渲染登录页），
    登录后同一 Session 自动携带 sessionid。
  * 匿名住客客户端与员工客户端是两个独立的 requests.Session——
    员工登录不得污染匿名公开面用例。
  * 测试账号一律从环境变量读取：PMS_STAFF_USER / PMS_STAFF_PASSWORD；
    缺省值仅为本机演示账号（demo_owner / Demo@Owner2026），可被完全覆盖，
    不在代码/日志/断言中记录任何真实口令或令牌明文。
  * 公开面限流（§6.3）：预订创建 IP 10 次/10 分钟、同手机号 3 次/10 分钟、
    登录 IP+用户名 5 次/5 分钟——本套件每轮运行最多创建 1 笔预订（含幂等
    重放共 2 次写请求）、登录 1 次，正常运行不会触发限流。
"""

import re
import uuid
from os import environ

import pytest
import requests


def _env(name, default):
    value = environ.get(name)
    return value if value else default


_CSRF_INPUT_RE = re.compile(r'name="csrfmiddlewaretoken"[^>]*value="([^"]+)"')


class HttpApi:
    """requests.Session 薄封装：统一相对路径、超时、CSRF 头与 JSON 解析。"""

    def __init__(self, base_url):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self.timeout = 15
        self.csrf_token = ""

    def url(self, path):
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return self.base_url + path

    def get(self, path, **kwargs):
        kwargs.setdefault("timeout", self.timeout)
        return self.session.get(self.url(path), **kwargs)

    def post(self, path, **kwargs):
        kwargs.setdefault("timeout", self.timeout)
        # CSRF_USE_SESSIONS=True：写请求统一携带 X-CSRFToken（服务端按会话校验）
        if self.csrf_token:
            headers = kwargs.setdefault("headers", {})
            headers.setdefault("X-CSRFToken", self.csrf_token)
        return self.session.post(self.url(path), **kwargs)

    def post_json(self, path, payload=None, headers=None):
        return self.post(
            path,
            json=payload if payload is not None else {},
            headers=headers or {},
        )

    def ensure_csrf(self, path="/reserve/"):
        """从含 {% csrf_token %} 的公开页获取/刷新 CSRF 令牌。

        登录、退出会轮换令牌；换绑会话后必须重新调用本方法。
        """
        page = self.get(path)
        assert page.status_code == 200, f"无法获取 CSRF 令牌：GET {path} 返回 {page.status_code}"
        match = _CSRF_INPUT_RE.search(page.text)
        assert match, f"页面 {path} 未渲染 csrfmiddlewaretoken"
        self.csrf_token = match.group(1)
        return self.csrf_token


@pytest.fixture(scope="session")
def base_url():
    """被测实例地址：PMS_BASE_URL，默认 http://127.0.0.1:8000。"""
    return _env("PMS_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


@pytest.fixture(scope="session")
def api(base_url):
    """匿名（住客视角）HTTP 客户端，独立于员工会话。"""
    return HttpApi(base_url)


@pytest.fixture(scope="session")
def staff_credentials():
    """员工测试账号（环境变量注入，缺省为本机演示账号）。"""
    return {
        "username": _env("PMS_STAFF_USER", "demo_owner"),
        "password": _env("PMS_STAFF_PASSWORD", "Demo@Owner2026"),
    }


def _form_login(api: HttpApi, username: str, password: str):
    """POST /login/ 表单流：返回已携带 PMS 会话 Cookie 的同一客户端。

    成功 = 302（跳 /home/）；失败 = 200 回渲染登录页。
    不打印/记录任何凭据、Cookie 或令牌明文。
    """
    login_page = api.get("/login/")
    assert login_page.status_code == 200, (
        f"登录页不可达：GET /login/ 返回 {login_page.status_code}"
        "（请确认 PMS_BASE_URL 指向已启动实例）"
    )
    match = _CSRF_INPUT_RE.search(login_page.text)
    assert match, "登录页未渲染 csrfmiddlewaretoken，无法完成表单登录"
    api.csrf_token = match.group(1)
    # 提交登录表单（同一 Session 保证服务端会话中的 CSRF 令牌可校验）
    response = api.post(
        "/login/",
        data={
            "username": username,
            "password": password,
            "csrfmiddlewaretoken": match.group(1),
        },
        headers={"Referer": api.url("/login/")},
        allow_redirects=False,
    )
    # 登录失败时 Django 回渲染 200 登录页；给出可操作提示，
    # 但不回显提交的账号口令。
    assert response.status_code == 302, (
        "员工登录失败：请检查 PMS_STAFF_USER/PMS_STAFF_PASSWORD 对应账号在该实例"
        "是否存在、在职且有有效角色授权（或是否触发了登录限流）"
    )
    # 登录成功后 Django 轮换 CSRF 令牌：从公开页重新获取
    api.ensure_csrf("/reserve/")
    return api


@pytest.fixture(scope="session")
def staff_session(base_url, staff_credentials):
    """已登录员工客户端（session 级：每轮运行只登录一次，避免登录限流）。

    与匿名 api 客户端互相独立。
    """
    staff = HttpApi(base_url)
    _form_login(staff, staff_credentials["username"], staff_credentials["password"])
    # 认证 smoke 基线：登录后主界面必须可达
    home = staff.get("/home/")
    assert home.status_code == 200, "登录后 GET /home/ 非常 200，员工会话未生效"
    return staff


@pytest.fixture(scope="module")
def unique_phone():
    """每模块唯一手机号：避开 phone_hash 限流桶与既有住客数据。"""
    return "1" + uuid.uuid4().hex[:10]


@pytest.fixture(scope="module")
def idempotency_key():
    """每模块唯一幂等键（uuid 前缀便于在日志中识别测试流量）。"""
    return "pytest-p0-" + uuid.uuid4().hex
