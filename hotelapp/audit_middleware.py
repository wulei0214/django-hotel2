import logging

audit = logging.getLogger("hotel.audit")


class AuditMiddleware:
    """操作审计：记录所有写操作（POST）与删除请求的操作者、路径和结果状态，写入 logs/audit.log"""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        is_mutation = request.method == "POST" or request.path.startswith("/delete_")
        if is_mutation and not request.path.startswith("/admin/login"):
            user = "anonymous"
            if hasattr(request, "user") and request.user.is_authenticated:
                user = request.user.username
            audit.info("%s %s -> %s (user=%s)",
                       request.method, request.get_full_path(), response.status_code, user)
        return response
