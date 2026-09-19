"""领域错误。携带机器可读 code，HTTP 层据此映射状态码。"""


class ClinicError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class NotFound(ClinicError):
    def __init__(self, message="资源不存在"):
        super().__init__("not_found", message, 404)


class Conflict(ClinicError):
    """并发改期：资源版本已过期或时间槽在重放后不再成立。"""

    def __init__(self, code, message):
        super().__init__(code, message, 409)


class ValidationFailed(ClinicError):
    def __init__(self, message, code="validation_failed"):
        super().__init__(code, message, 422)


class Forbidden(ClinicError):
    def __init__(self, message="无权访问该资源"):
        super().__init__("forbidden", message, 403)
