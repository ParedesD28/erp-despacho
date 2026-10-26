"""Lectura de filas independiente del cursor (tupla o RealDictCursor)."""
from __future__ import annotations

from typing import Any, Iterable


def row_value(row: Any, key: str | None = None, index: int = 0) -> Any:
    """Valor de una fila: por nombre si es dict-like, por posición si es tupla."""
    if row is None:
        return None
    if hasattr(row, "keys"):
        if key is not None and key in row.keys():
            return row[key]
        values = list(row.values()) if hasattr(row, "values") else []
        return values[index] if index < len(values) else None
    return row[index]


def first_value(row: Any, key: str | None = None) -> Any:
    return row_value(row, key, 0)


def names_set(rows: Iterable[Any], key: str = "column_name", *, lower: bool = True) -> set[str]:
    out: set[str] = set()
    for row in rows:
        value = row_value(row, key, 0)
        if value is not None:
            out.add(str(value).lower() if lower else str(value))
    return out


def rows_as_dicts(cur: Any, rows: Iterable[Any] | None = None) -> list[dict]:
    """Convierte filas (tupla o dict) a dict usando cur.description si hace falta."""
    rows = list(cur.fetchall() if rows is None else rows)
    if not rows:
        return []
    if hasattr(rows[0], "keys"):
        return [dict(r) for r in rows]
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in rows]
