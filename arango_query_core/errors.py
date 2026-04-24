from __future__ import annotations


class CoreError(Exception):
    def __init__(self, message: str, *, code: str = "CORE_ERROR"):
        super().__init__(message)
        self.code = code
