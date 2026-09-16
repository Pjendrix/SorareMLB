"""Archiv bez horního limitu, rozdělený po čtvrtletích.

Hodnota v Upstash free smí mít max. 1 MB, takže celá historie účtu se do
jednoho klíče nevejde. Záznamy se ukládají do klíčů po čtvrtletích
(`<prefix>:2024Q3`) a seznam čtvrtletí drží index `<prefix>:index`.
Stahování celé historie navazuje přes kurzor uložený v `<prefix>:cursor`.
"""
from __future__ import annotations

import time
from collections import defaultdict

from .store import get_store


def _shard(date_value) -> str:
    text = str(date_value or "")
    if len(text) >= 7 and text[:4].isdigit():
        quarter = (int(text[5:7]) - 1) // 3 + 1 if text[5:7].isdigit() else 1
        return f"{text[:4]}Q{quarter}"
    return "unknown"


class Archive:
    def __init__(self, prefix: str, date_field: str):
        self.prefix = prefix
        self.date_field = date_field

    # ------------------------------------------------------------ zápis

    def merge(self, rows: list[dict]) -> int:
        """Přidá/aktualizuje záznamy podle `id`. Vrací počet nových."""
        store = get_store()
        groups: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            if r.get("id"):
                groups[_shard(r.get(self.date_field))].append(r)

        index = set(self.shards())
        new = 0
        for shard, items in groups.items():
            key = f"{self.prefix}:{shard}"
            current = {r["id"]: r for r in store.get_json(key, []) or []}
            new += sum(1 for r in items if r["id"] not in current)
            current.update({r["id"]: r for r in items})
            store.set(key, list(current.values()))
            index.add(shard)
        store.set(f"{self.prefix}:index", sorted(index))
        return new

    def known_ids(self) -> set[str]:
        return {r["id"] for r in self.rows()}

    # ------------------------------------------------------------ čtení

    def shards(self) -> list[str]:
        return get_store().get_json(f"{self.prefix}:index", []) or []

    def rows(self) -> list[dict]:
        store = get_store()
        out = []
        for shard in self.shards():
            out += store.get_json(f"{self.prefix}:{shard}", []) or []
        return sorted(out, key=lambda r: str(r.get(self.date_field) or ""), reverse=True)

    # ------------------------------------------------------------ kurzor

    def cursor_state(self, scope: str) -> dict:
        return get_store().get_json(f"{self.prefix}:cursor:{scope}", {}) or {}

    def save_cursor(self, scope: str, state: dict) -> None:
        get_store().set(f"{self.prefix}:cursor:{scope}", state)

    def reset(self, scope: str) -> None:
        get_store().delete(f"{self.prefix}:cursor:{scope}")


def paginate(
    fetch_page,
    archive: Archive,
    scope: str,
    full: bool,
    budget_seconds: float = 40.0,
    max_pages: int = 6,
) -> dict:
    """Společná logika stahování.

    * `full=False`: jde od nejnovějších a skončí na první stránce, kde už
      všechno známe (nebo po `max_pages`).
    * `full=True`: pokračuje od uloženého kurzoru, dokud nedojde na konec
      historie nebo nevyprší časový rozpočet. Volá se opakovaně.

    `fetch_page(cursor)` vrací (řádky, next_cursor | None).
    """
    started = time.monotonic()
    state = archive.cursor_state(scope) if full else {}
    if full and state.get("done"):
        return {"scope": scope, "done": True, "fetched": 0, "new": 0, "pages": state.get("pages", 0)}

    cursor = state.get("cursor")
    pages = state.get("pages", 0)
    fetched = new = 0
    done = False
    known = None if full else archive.known_ids()

    for i in range(10_000 if full else max_pages):
        rows, next_cursor = fetch_page(cursor)
        fetched += len(rows)
        page_new = archive.merge(rows)
        new += page_new
        pages += 1
        cursor = next_cursor
        if not next_cursor:
            done = True
            break
        if known is not None and rows and all(r["id"] in known for r in rows):
            break
        if full:
            archive.save_cursor(scope, {"cursor": cursor, "pages": pages, "done": False})
            if time.monotonic() - started > budget_seconds:
                break

    if full:
        archive.save_cursor(scope, {"cursor": cursor, "pages": pages, "done": done})
    return {"scope": scope, "done": done if full else True, "fetched": fetched, "new": new, "pages": pages}
