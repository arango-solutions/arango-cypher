from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .aql import AqlQuery


@dataclass
class AqlExecutor:
    db: Any  # python-arango Database

    def execute(self, query: AqlQuery, *, batch_size: int | None = None, **kwargs: Any) -> Any:
        aql = self.db.aql
        return aql.execute(query.text, bind_vars=query.bind_vars, batch_size=batch_size, **kwargs)

