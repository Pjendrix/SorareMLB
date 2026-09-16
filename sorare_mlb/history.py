"""Historie, kterou si aplikace buduje sama.

Sorare API vrací jen „probíhající a nedávné“ sestavy, takže delší historii
ukládáme do Redisu: po každém běhu automatu záznam o sestavách a jednou
denně snímek sbírky. Ukládají se agregáty, ne syrové odpovědi — Upstash
free plán má limit na velikost databáze.
"""
from __future__ import annotations

from datetime import date, datetime

from .store import K_HIST_JOBS, K_HIST_SNAPSHOT_DAY, K_HIST_SNAPSHOTS, get_store

MAX_JOBS = 200
MAX_SNAPSHOTS = 400


def record_job(job) -> None:
    """Zapíše dokončený běh. Chyba zápisu nesmí shodit pipeline."""
    try:
        fixture = job.fixture or {}
        entry = {
            "job_id": job.id,
            "at": job.updated_at or datetime.now().isoformat(timespec="seconds"),
            "sport": "mlb",
            "mode": job.mode,
            "state": job.state,
            "game_week": fixture.get("gameWeek"),
            "fixture": fixture.get("slug"),
            "lineups": [
                {
                    "tournament": lu.get("tournament_name"),
                    "index": lu.get("index", 0),
                    "projected": round(float(lu.get("total_projected") or 0), 1),
                    "players": [s.get("player") for s in lu.get("slots") or []],
                }
                for lu in job.lineups or []
            ],
            "submitted_ok": sum(1 for s in job.submitted or [] if s.get("ok")),
            "submitted_failed": sum(1 for s in job.submitted or [] if not s.get("ok")),
            "blockers": sum(1 for i in job.issues or [] if i.get("severity") == "blocker"),
            "error": job.error,
        }
        get_store().push_capped(K_HIST_JOBS, entry, MAX_JOBS)
    except Exception:  # noqa: BLE001
        pass


def jobs(limit: int = 50) -> list[dict]:
    return get_store().read_list(K_HIST_JOBS, limit)


def snapshot_if_due(sport: str, summary: dict, today: date | None = None) -> bool:
    """Uloží denní snímek sbírky, pokud dnes ještě neproběhl."""
    today = today or date.today()
    store = get_store()
    day_key = K_HIST_SNAPSHOT_DAY.format(sport=sport)
    if store.get(day_key) == today.isoformat():
        return False
    store.push_capped(
        K_HIST_SNAPSHOTS.format(sport=sport),
        {
            "date": today.isoformat(),
            "count": summary.get("count"),
            "avg_l15": summary.get("avg_l15"),
            "idle": summary.get("idle_count"),
            "by_rarity": summary.get("by_rarity"),
        },
        MAX_SNAPSHOTS,
    )
    store.set(day_key, today.isoformat(), ttl_seconds=2 * 24 * 3600)
    return True


def snapshots(sport: str, limit: int = 120) -> list[dict]:
    """Snímky od nejstaršího po nejnovější (pro graf)."""
    return list(reversed(get_store().read_list(K_HIST_SNAPSHOTS.format(sport=sport), limit)))
