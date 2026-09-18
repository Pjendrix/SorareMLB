"""Spouštění podle skutečné uzávěrky gameweeku.

GitHub Actions volá /api/tick často (každých 15 min). Tick se podívá na
nejbližší `cutOffDate` z API a rozhodne:

* build   — v okně `schedule.build_before_minutes` před uzávěrkou postaví
            a (podle CRON_MODE) odešle sestavy,
* recheck — v okně `schedule.recheck_before_minutes` postaví sestavy znovu
            s čerstvými startéry a přepíše je (createOrUpdate). Běží jen
            v režimu auto a jen když build doběhl.

Každá fáze běží pro daný fixture nejvýš jednou (klíč v Redisu). GitHub
cron umí mít zpoždění 5–15 min, proto jsou okna široká a rozhoduje se
podle zbývajícího času, ne podle přesné minuty.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from . import runner
from .store import K_TICK_DONE, K_TICK_REMINDER, get_store


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def plan(config, boards: list[dict], now: datetime | None = None) -> dict:
    """Čistá funkce: co by tick teď udělal. Bez vedlejších efektů (testy, UI)."""
    now = now or datetime.now(timezone.utc)
    build_before = float(config.get_path("schedule.build_before_minutes", 60))
    recheck_before = float(config.get_path("schedule.recheck_before_minutes", 20))
    min_left = float(config.get_path("schedule.min_minutes_left", 3))

    upcoming = [b for b in boards if (_parse(b.get("cutOffDate")) or now) > now]
    if not upcoming:
        return {"action": "idle", "reason": "žádná budoucí uzávěrka"}
    fixture, fixture_boards = runner.nearest_fixture(upcoming)
    cutoff = min(_parse(b.get("cutOffDate")) for b in fixture_boards if _parse(b.get("cutOffDate")))
    minutes = (cutoff - now).total_seconds() / 60
    out = {
        "fixture": fixture.get("slug"),
        "game_week": fixture.get("gameWeek"),
        "cutoff": cutoff.isoformat(),
        "minutes_left": round(minutes, 1),
        "build_at": (cutoff.timestamp() - build_before * 60),
        "recheck_at": (cutoff.timestamp() - recheck_before * 60),
    }
    if minutes < min_left:
        return {**out, "action": "idle", "reason": "na odeslání je pozdě"}
    if minutes <= recheck_before:
        return {**out, "action": "recheck"}
    if minutes <= build_before:
        return {**out, "action": "build"}
    return {**out, "action": "idle", "reason": "ještě není čas"}


def tick(config, client, token_status: dict) -> dict:
    """Jeden tik. Vrací, co udělal (pro log v GitHub Actions)."""
    from . import notify

    store = get_store()
    today = datetime.now(timezone.utc).date().isoformat()

    # Připomínka tokenu jednou denně, ne při každém tiku.
    if token_status.get("needs_login") and store.acquire(K_TICK_REMINDER.format(day=today), 26 * 3600):
        if token_status.get("authenticated"):
            notify.notify(f"🔑 Sorare token platí ještě {token_status.get('days_left')} dní — obnov ho na /login.")
        else:
            notify.notify("🔑 **Sorare token vypršel.** Přihlas se na /login, jinak se sestavy neodešlou.")
    if not token_status.get("authenticated"):
        return {"action": "idle", "reason": "chybí token"}

    boards = client.fetch_leaderboards()
    decision = plan(config, boards)
    action = decision["action"]
    if action == "idle":
        return decision

    running = runner.latest_job()
    if running and running.state not in runner.TERMINAL:
        runner.advance(running, config)
        return {**decision, "action": "resumed", "job_id": running.id}

    mode = os.environ.get("CRON_MODE", "auto")
    fixture = decision["fixture"] or "?"
    ttl = 4 * 24 * 3600

    if action == "recheck":
        # Recheck má smysl jen u automatického odesílání a jen po buildu.
        built = store.get(K_TICK_DONE.format(fixture=fixture, phase="build"))
        if mode != "auto" or not built:
            action = "build"
        elif not store.acquire(K_TICK_DONE.format(fixture=fixture, phase="recheck"), ttl):
            return {**decision, "action": "idle", "reason": "recheck už proběhl"}
        else:
            job = runner.create_job(mode="auto", overwrite=True, trigger="tick-recheck")
            runner.advance(job, config)
            return {**decision, "job_id": job.id}

    if not store.acquire(K_TICK_DONE.format(fixture=fixture, phase="build"), ttl):
        return {**decision, "action": "idle", "reason": "build už proběhl"}
    job = runner.create_job(mode=mode, trigger="tick-build")
    runner.advance(job, config)
    return {**decision, "action": "build", "job_id": job.id}
