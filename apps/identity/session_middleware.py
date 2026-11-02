"""Path-isolated authentication and CSRF cookies for PMS and Django Admin.

The two entry points intentionally keep using the same Django User table and
session backend, but they must not overwrite each other's browser credentials.
PMS keeps the historical cookie names for compatibility; /admin/ gets a
separate cookie namespace.
"""
import time

from django.conf import settings
from django.contrib.sessions.backends.base import UpdateError
from django.contrib.sessions.exceptions import SessionInterrupted
from django.contrib.sessions.middleware import SessionMiddleware
from django.middleware.csrf import CsrfViewMiddleware
from django.utils.cache import patch_vary_headers
from django.utils.http import http_date


def is_admin_request(request):
    """Return True for the Django Admin route, including its login/logout URLs."""
    path = request.path_info or request.path
    return path == "/admin" or path.startswith("/admin/")


class SplitSessionMiddleware(SessionMiddleware):
    """Select a dedicated session cookie for Django Admin requests."""

    def cookie_name_for_request(self, request):
        if is_admin_request(request):
            return settings.ADMIN_SESSION_COOKIE_NAME
        return settings.SESSION_COOKIE_NAME

    def process_request(self, request):
        cookie_name = self.cookie_name_for_request(request)
        request._session_cookie_name = cookie_name
        request.session = self.SessionStore(request.COOKIES.get(cookie_name))

    def process_response(self, request, response):
        try:
            accessed = request.session.accessed
            modified = request.session.modified
            empty = request.session.is_empty()
        except AttributeError:
            return response

        cookie_name = getattr(
            request, "_session_cookie_name", self.cookie_name_for_request(request)
        )
        if cookie_name in request.COOKIES and empty:
            response.delete_cookie(
                cookie_name,
                path=settings.SESSION_COOKIE_PATH,
                domain=settings.SESSION_COOKIE_DOMAIN,
                samesite=settings.SESSION_COOKIE_SAMESITE,
            )
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
                    response.set_cookie(
                        cookie_name,
                        request.session.session_key,
                        max_age=max_age,
                        expires=expires,
                        domain=settings.SESSION_COOKIE_DOMAIN,
                        path=settings.SESSION_COOKIE_PATH,
                        secure=settings.SESSION_COOKIE_SECURE or None,
                        httponly=settings.SESSION_COOKIE_HTTPONLY or None,
                        samesite=settings.SESSION_COOKIE_SAMESITE,
                    )
        return response
