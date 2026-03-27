from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import CoreError


@dataclass
class AqlFragment:
    text: str
    bind_vars: dict[str, Any] = field(default_factory=dict)

    def __add__(self, other: "AqlFragment") -> "AqlFragment":
        if not isinstance(other, AqlFragment):
            return NotImplemented
        text = self.text.rstrip()
        other_text = other.text.lstrip()
        joined = (text + "\n" + other_text).strip() if text and other_text else (text + other_text).strip()

        merged: dict[str, Any] = dict(self.bind_vars)
        for k, v in other.bind_vars.items():
            if k in merged and merged[k] != v:
                raise CoreError(f"Bind var collision for {k!r}", code="BIND_VAR_COLLISION")
            merged[k] = v
        return AqlFragment(text=joined, bind_vars=merged)


@dataclass(frozen=True)
class AqlQuery:
    text: str
    bind_vars: dict[str, Any]
    debug: dict[str, Any] | None = None

