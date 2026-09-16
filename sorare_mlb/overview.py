"""Data pro přehledové stránky.

Každý blok se počítá zvlášť a chyba jednoho neshodí ostatní — stránka pak
ukáže, co se načíst povedlo, a u zbytku napíše proč ne.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import history
from .auth import token_status
from .client import SorareClient
from .models import Config
from .sports import ADAPTERS, get_adapter
from .store import K_OVERVIEW, get_store

SPORT_BY_ENUM = {cls.sport: key for key, cls in ADAPTERS.items()}


def _error(exc: Exception) -> dict:
    return {"error": f"{type(exc).__name__}: {exc}"[:400]}


def allowed_rarities(key: str, config: Config) -> list[str] | None:
    """Rarity turnajů, které se mají ukazovat (None = všechny)."""
    values = config.get_path(f"sports.{key}.board_rarities")
    return [str(v).lower() for v in values] if values else None


def _rarity_ok(key: str, rarity, config: Config) -> bool:
    allowed = allowed_rarities(key, config)
    return not allowed or not rarity or str(rarity).lower() in allowed


def enabled_sports(config: Config) -> list[str]:
    sports = config.get("sports") or {}
    return [k for k in ADAPTERS if (sports.get(k) or {}).get("enabled", True)]


# ------------------------------------------------------------------ sbírka


def sport_overview(key: str, config: Config, refresh: bool = False) -> dict:
    adapter = get_adapter(key)
    store = get_store()
    cache_key = K_OVERVIEW.format(sport=key)
    if not refresh:
        cached = store.get_json(cache_key)
        if cached:
            return cached

    client = SorareClient(config)
    cards, truncated = adapter.fetch_cards(client, config)
    summary = adapter.summarize(cards, config)
    summary.update(
        {
            "sport": key,
            "label": adapter.label,
            "truncated": truncated,
            "lineups_supported": adapter.lineups_supported,
            "generated_at": datetime.now().isoformat(timespec="minutes"),
        }
    )
    try:
        history.snapshot_if_due(key, summary)
    except Exception:  # noqa: BLE001 — snímek není kritický
        pass

    ttl = int(config.get_path("overview.cache_minutes", 30)) * 60
    store.set(cache_key, summary, ttl_seconds=ttl)
    return summary


# ------------------------------------------------------------------ turnaje


def upcoming(config: Config) -> dict:
    """Otevřené turnaje seskupené podle sportu a fixture."""
    client = SorareClient(config)
    boards = client.fetch_all_upcoming(cache_seconds=300)
    out: dict[str, list] = {k: [] for k in ADAPTERS}
    fixtures: dict[str, dict] = {}

    # `mySo5LineupsCount` u otevřených leaderboardů někdy vrací 0, i když
    # sestava existuje. Druhým zdrojem jsou moje probíhající sestavy.
    mine_by_slug: dict[str, int] = {}
    try:
        for lu in (client.fetch_recent_lineups().get("lineups") or []):
            slug = (lu.get("so5Leaderboard") or {}).get("slug")
            if slug:
                mine_by_slug[slug] = mine_by_slug.get(slug, 0) + 1
    except Exception:  # noqa: BLE001
        pass

    for b in boards:
        fixture = b.get("so5Fixture") or {}
        key = SPORT_BY_ENUM.get(str(fixture.get("sport") or "").upper())
        if not key or not _rarity_ok(key, b.get("rarityType"), config):
            continue
        b = dict(b, mySo5LineupsCount=max(
            int(b.get("mySo5LineupsCount") or 0), mine_by_slug.get(b.get("slug"), 0)
        ))
        out[key].append(
            {
                "slug": b.get("slug"),
                "name": b.get("displayName"),
                "rarity": b.get("rarityType"),
                "cutoff": b.get("cutOffDate"),
                "mine": int(b.get("mySo5LineupsCount") or 0),
                "game_week": fixture.get("gameWeek") or b.get("gameWeek"),
            }
        )
        fx = fixtures.setdefault(
            key + ":" + str(fixture.get("slug")),
            {
                "sport": key,
                "slug": fixture.get("slug"),
                "game_week": fixture.get("gameWeek"),
                "start": fixture.get("startDate"),
                "end": fixture.get("endDate"),
                "cutoff": b.get("cutOffDate"),
                "boards": 0,
                "with_lineup": 0,
            },
        )
        fx["boards"] += 1
        fx["with_lineup"] += 1 if int(b.get("mySo5LineupsCount") or 0) else 0
        if b.get("cutOffDate") and (not fx["cutoff"] or b["cutOffDate"] < fx["cutoff"]):
            fx["cutoff"] = b["cutOffDate"]

    tracked = _tracked(config)
    for key, rows in out.items():
        rows.sort(key=lambda r: (not _is_tracked(r, tracked.get(key, [])), str(r["cutoff"])))
        for r in rows:
            r["tracked"] = _is_tracked(r, tracked.get(key, []))

    return {
        "boards": out,
        "fixtures": sorted(fixtures.values(), key=lambda f: str(f["cutoff"])),
    }


def _tracked(config: Config) -> dict[str, list[dict]]:
    mlb = config.get("tournaments") or []
    football = config.get_path("sports.football.tournaments", []) or []
    return {"mlb": mlb, "football": football}


def _is_tracked(board: dict, specs: list[dict]) -> bool:
    slug = str(board.get("slug") or "")
    rarity = str(board.get("rarity") or "").lower()
    return any(
        s.get("slug_contains", "\0") in slug
        and (not s.get("rarity") or s["rarity"] == rarity)
        for s in specs
    )


# ------------------------------------------------------------------ sestavy


def recent_lineups(config: Config) -> dict:
    client = SorareClient(config)
    raw = client.fetch_recent_lineups()
    rows = []
    for lu in raw.get("lineups") or []:
        board = lu.get("so5Leaderboard") or {}
        fixture = board.get("so5Fixture") or {}
        rankings = lu.get("so5Rankings") or []
        best = rankings[0] if rankings else {}
        sport = SPORT_BY_ENUM.get(str(fixture.get("sport") or "").upper(), "?")
        if sport != "?" and not _rarity_ok(sport, board.get("rarityType"), config):
            continue
        rows.append(
            {
                "id": lu.get("id"),
                "board_slug": board.get("slug"),
                "sport": sport,
                "tournament": board.get("displayName") or board.get("slug"),
                "rarity": board.get("rarityType"),
                "game_week": fixture.get("gameWeek"),
                "start": fixture.get("startDate"),
                "end": fixture.get("endDate"),
                "score": best.get("score"),
                "ranking": best.get("ranking"),
                "live": _is_live(fixture),
            }
        )
    rows.sort(key=lambda r: str(r["end"] or ""), reverse=True)

    # Probíhající gameweek po sportech: kolik sestav a v jakých soutěžích.
    current: dict[str, dict] = {}
    for r in rows:
        if not r["live"]:
            continue
        c = current.setdefault(r["sport"], {"sport": r["sport"], "game_week": r["game_week"],
                                            "end": r["end"], "lineups": 0, "tournaments": {}})
        c["lineups"] += 1
        c["tournaments"][r["tournament"]] = c["tournaments"].get(r["tournament"], 0) + 1
        if r["score"] is not None:
            c["score"] = round(c.get("score", 0) + float(r["score"]), 1)

    return {
        "current": list(current.values()),
        "variant": raw.get("variant"),
        "has_scores": raw.get("variant") == "RecentLineupsScored",
        "has_sport": raw.get("variant") != "RecentLineupsBasic",
        "lineups": rows,
    }


def _is_live(fixture: dict) -> bool:
    try:
        now = datetime.now(timezone.utc)
        start = datetime.fromisoformat(str(fixture["startDate"]).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(fixture["endDate"]).replace("Z", "+00:00"))
        return start <= now <= end
    except (KeyError, TypeError, ValueError):
        return False


# ------------------------------------------------------------------ dashboard


def dashboard(config: Config) -> dict:
    """Lehká část dashboardu. Sbírky se dotahují zvlášť (/api/overview/…)."""
    from . import runner

    data: dict = {"auth": token_status(), "sports": enabled_sports(config)}

    job = runner.latest_job()
    data["job"] = (
        {
            "id": job.id,
            "state": job.state,
            "mode": job.mode,
            "updated_at": job.updated_at,
            "lineups": len(job.lineups or []),
            "blockers": sum(1 for i in job.issues or [] if i.get("severity") == "blocker"),
            "warnings": sum(1 for i in job.issues or [] if i.get("severity") != "blocker"),
        }
        if job
        else None
    )

    if not data["auth"]["authenticated"]:
        data["upcoming"] = {"error": "Nejsi přihlášen k Sorare."}
        data["recent"] = {"error": "Nejsi přihlášen k Sorare."}
        return data

    try:
        data["upcoming"] = upcoming(config)
    except Exception as exc:  # noqa: BLE001
        data["upcoming"] = _error(exc)
    try:
        data["recent"] = recent_lineups(config)
    except Exception as exc:  # noqa: BLE001
        data["recent"] = _error(exc)
    data["history"] = history.jobs(5)
    return data
