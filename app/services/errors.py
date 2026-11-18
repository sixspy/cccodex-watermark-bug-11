class ServiceError(Exception):
    """业务错误：带机器可核的拒因码与白话说明。"""

    def __init__(
        self,
        message: str,
        status_code: int = 400,
        reason_code: str = "BAD_REQUEST",
        details: dict | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.reason_code = reason_code
        self.details = details or {}

    def payload(self) -> dict:
        return {
            "ok": False,
            "error": self.message,
            "reason_code": self.reason_code,
            "details": self.details,
        }
