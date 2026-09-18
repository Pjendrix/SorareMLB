"""Kalibrace: projekce vs. skutečnost po skončení gameweeku.

Při odeslání se uloží, co model u každé karty čekal. Po skončení gameweeku
se ze skóre hráčů (stejný dotaz jako karty, žádné volání navíc) spočítá,
kolik doopravdy uhráli. Bez tohohle se nedá poznat, jestli váhy v configu
něco dělají.

Skutečné body karty = součet (nebo průměr, viz `projection.sum_over_games`)
skóre hráče v zápasech s datem uvnitř gameweeku × bonus karty. Hráče, které
už nemáš, API nevrátí — jejich řádky zůstanou bez skutečnosti.
"""
from __future__ import annotations

import statistics
from datetime import date, datetime, timedelta, timezone

from .store import K_CALIBRATION, K_CARDS, get_store

LIMIT = 150


def _load() -> list[dict]:
    data = get_store().get_json(K_CALIBRATION) or []
    return data if isinstance(data, list) else []


def _save(entries: list[dict]) -> None:
    get_store().set(K_CALIBRATION, entries[:LIMIT])


def record(job) -> int:
    """Uloží odeslané sestavy jobu. Vrací počet nových záznamů."""
    fixture = job.fixture or {}
    ok = {(s["tournament_slug"], s.get("index", 0)): s for s in job.submitted if s.get("ok")}
    entries = _load()
    known = {(e.get("fixture"), e.get("tournament_slug"), e.get("index")) for e in entries}
    added = 0
    for lu in job.lineups:
        key = (lu["tournament_slug"], lu.get("index", 0))
        if key not in ok:
            continue
        ident = (fixture.get("slug"), lu["tournament_slug"], lu.get("index", 0))
        if ident in known:
            # Druhý běh těsně před uzávěrkou přepsal sestavu — platí nová.
            entries = [e for e in entries if (e.get("fixture"), e.get("tournament_slug"), e.get("index")) != ident]
        entries.insert(0, {
            "fixture": fixture.get("slug"),
            "game_week": fixture.get("gameWeek"),
            "start": fixture.get("startDate"),
            "end": fixture.get("endDate"),
            "tournament": lu["tournament_name"],
            "tournament_slug": lu["tournament_slug"],
            "index": lu.get("index", 0),
            "lineup_id": ok[key].get("lineup_id"),
            "target": lu.get("target_score"),
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "evaluated": False,
            "slots": [
                {
                    "slot": s["slot"],
                    "player": s["player"],
                    "player_slug": s.get("player_slug"),
                    "card_slug": s["card_slug"],
                    "projected": s.get("projected"),
                    "floor": s.get("floor"),
                    "ceiling": s.get("ceiling"),
                    "games": s.get("games"),
                    "actual": None,
                }
                for s in lu["slots"]
            ],
        })
        added += 1
    _save(entries)
    return added


def _parse_day(value) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def evaluate(config, client=None) -> int:
    """Doplní skutečné body u skončených gameweeků. Vrací počet vyhodnocených."""
    entries = _load()
    today = datetime.now(timezone.utc).date()
    pending = [
        e for e in entries
        if not e.get("evaluated") and (_parse_day(e.get("end")) or today) < today
    ]
    if not pending:
        return 0

    if client is not None:
        client.fetch_cards(use_cache=False)  # naplní K_CARDS čerstvými skóre
    nodes = get_store().get_json(K_CARDS) or []

    from .client import _power_mult

    by_player: dict[str, list[tuple[date, float]]] = {}
    power: dict[str, float] = {}
    for node in nodes:
        player = node.get("anyPlayer") or {}
        power[node.get("slug", "")] = _power_mult(node.get("cardPower"))
        slug = player.get("slug")
        if not slug or slug in by_player:
            continue
        rows = []
        for g in player.get("playerGameScores") or []:
            if not g or g.get("score") is None:
                continue
            day = _parse_day((g.get("anyGame") or {}).get("date"))
            if day:
                rows.append((day, float(g["score"])))
        by_player[slug] = rows

    sum_games = bool(config.get_path("projection.sum_over_games", True))
    done = 0
    for entry in pending:
        start, end = _parse_day(entry.get("start")), _parse_day(entry.get("end"))
        if not start or not end:
            entry["evaluated"] = True
            continue
        # Zápasy po půlnoci UTC patří ještě k poslednímu dni gameweeku.
        end_incl = end + timedelta(days=1)
        for slot in entry["slots"]:
            rows = by_player.get(slot.get("player_slug") or "")
            if rows is None:
                continue
            played = [score for day, score in rows if start <= day <= end_incl]
            if not played:
                slot["actual"] = 0.0
                continue
            raw = sum(played) if sum_games else statistics.fmean(played)
            slot["actual"] = round(raw * power.get(slot["card_slug"], 1.0), 2)
            slot["played"] = len(played)
        entry["evaluated"] = True
        done += 1
    _save(entries)
    return done


def summary() -> dict:
    """Data pro stránku kalibrace."""
    entries = _load()
    points, lineups = [], []
    for e in entries:
        actual_total = 0.0
        complete = True
        for s in e["slots"]:
            if s.get("actual") is None:
                complete = False
                continue
            actual_total += s["actual"]
            points.append({
                "player": s["player"], "slot": s["slot"], "fixture": e.get("fixture"),
                "projected": s.get("projected") or 0.0, "actual": s["actual"],
                "inside": (s.get("floor") or 0) <= s["actual"] <= (s.get("ceiling") or 1e9),
            })
        proj_total = sum(s.get("projected") or 0 for s in e["slots"])
        lineups.append({
            "fixture": e.get("fixture"), "game_week": e.get("game_week"),
            "tournament": e["tournament"], "index": e.get("index", 0),
            "projected": round(proj_total, 1),
            "actual": round(actual_total, 1) if e.get("evaluated") else None,
            "complete": complete, "target": e.get("target"),
            "hit": (actual_total >= e["target"]) if e.get("target") and e.get("evaluated") else None,
        })

    stats: dict = {"count": len(points)}
    if points:
        errors = [p["actual"] - p["projected"] for p in points]
        stats.update({
            "mae": round(statistics.fmean(abs(x) for x in errors), 2),
            "bias": round(statistics.fmean(errors), 2),
            "inside_range": round(sum(p["inside"] for p in points) / len(points), 3),
        })
        by_slot: dict[str, list[float]] = {}
        for p in points:
            by_slot.setdefault(p["slot"], []).append(abs(p["actual"] - p["projected"]))
        stats["mae_by_slot"] = {k: round(statistics.fmean(v), 2) for k, v in by_slot.items()}
    return {"stats": stats, "points": points[:600], "lineups": lineups}
