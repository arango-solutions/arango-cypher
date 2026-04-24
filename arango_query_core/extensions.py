from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .errors import CoreError


@dataclass(frozen=True)
class ExtensionPolicy:
    enabled: bool = False
    allowlist: set[str] | None = None
    denylist: set[str] | None = None

    def check_allowed(self, name: str) -> None:
        if not self.enabled:
            raise CoreError(f"Extensions are disabled (attempted: {name})", code="EXTENSIONS_DISABLED")
        if self.denylist and name in self.denylist:
            raise CoreError(f"Extension denied: {name}", code="EXTENSION_DENIED")
        if self.allowlist is not None and name not in self.allowlist:
            raise CoreError(f"Extension not in allowlist: {name}", code="EXTENSION_NOT_ALLOWED")


FunctionCompiler = Callable[[Any, Any], Any]
ProcedureCompiler = Callable[[Any, Any], Any]


class ExtensionRegistry:
    def __init__(self, *, policy: ExtensionPolicy | None = None):
        self.policy = policy or ExtensionPolicy()
        self._functions: dict[str, FunctionCompiler] = {}
        self._procedures: dict[str, ProcedureCompiler] = {}

    def register_function(self, name: str, compiler: FunctionCompiler) -> None:
        self._functions[name] = compiler

    def register_procedure(self, name: str, compiler: ProcedureCompiler) -> None:
        self._procedures[name] = compiler

    def compile_function(self, name: str, call_ast: Any, ctx: Any) -> Any:
        self.policy.check_allowed(name)
        fn = self._functions.get(name)
        if not fn:
            raise CoreError(f"Unknown extension function: {name}", code="UNKNOWN_EXTENSION")
        return fn(call_ast, ctx)

    def compile_procedure(self, name: str, call_ast: Any, ctx: Any) -> Any:
        self.policy.check_allowed(name)
        proc = self._procedures.get(name)
        if not proc:
            raise CoreError(f"Unknown extension procedure: {name}", code="UNKNOWN_EXTENSION")
        return proc(call_ast, ctx)
