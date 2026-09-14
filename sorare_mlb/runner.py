"""Pipeline jako stavový automat.

Proč takhle: Vercel Hobby ukončí funkci po 60 s, ale celý průchod (portfolio →
skóre → MLB kontext → optimalizace → odeslání) trvá déle. Job proto běží po
krocích. Každá invokace dělá práci, dokud nezbývá bezpečná rezerva, uloží stav
do Store a sama se zavolá znovu. Uživatel zatím jen pollinguje /api/status.

Stavy:
    QUEUED → CARDS → SCORES → MLB → OPTIMIZE → VALIDATE → SUBMIT → DONE
                                                      ↘ NEEDS_REVIEW
                                                      ↘ FAILED
"""
from __future__ import annotations

import logging
import os
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from types import SimpleNamespace
from datetime import datetime

import requests

from . import mlb, notify
from .client import SorareClient, SorareError, card_from_dict
from .models import Card, Config, Lineup, LineupSlot, Projection, Tournament
from .optimizer import LineupOptimizer, OptimizationError
from .projections import ProjectionEngine
from .store import K_CARDS, K_JOB, K_LAST_RESULT, K_LATEST_JOB, get_store
from .validator import LineupValidator

log = logging.getLogger(__name__)

# Kolik sekund necháme jako rezervu, než se funkce sama ukončí a předá štafetu.
BUDGET_SECONDS = float(os.environ.get("STEP_BUDGET_SECONDS", 40))
SCORE_BATCH = 25


@dataclass
class Job:
    id: str
    state: str = "QUEUED"
    mode: str = "auto"              # "auto" = smí odeslat, "propose" = jen navrhne
    created_at: str = ""
    updated_at: str = ""
    steps: list[str] = field(default_factory=list)
    error: str | None = None
    # Rozpracovaná data
    pending_player_slugs: list[str] = field(default_factory=list)
    scores: dict = field(default_factory=dict)
    mlb_context: dict = field(default_factory=dict)
    lineups: list[dict] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)
    submitted: list[dict] = field(default_factory=list)
    fixture: dict = field(default_factory=dict)

    def log_step(self, text: str) -> None:
        self.steps.append(f"{datetime.now():%H:%M:%S} {text}")
        self.updated_at = datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------- storage


def save(job: Job) -> None:
    store = get_store()
    store.set(K_JOB.format(job_id=job.id), asdict(job), ttl_seconds=6 * 3600)
    store.set(K_LATEST_JOB, job.id, ttl_seconds=6 * 3600)


def load(job_id: str) -> Job | None:
    data = get_store().get_json(K_JOB.format(job_id=job_id))
    if not data:
        return None
    return Job(**data)


def latest_job() -> Job | None:
    job_id = get_store().get(K_LATEST_JOB)
    return load(job_id) if job_id else None


def create_job(mode: str = "auto") -> Job:
    job = Job(
        id=uuid.uuid4().hex[:12],
        mode=mode,
        created_at=datetime.now().isoformat(timespec="seconds"),
    )
    job.log_step(f"Job vytvořen (režim: {mode})")
    save(job)
    return job


# --------------------------------------------------------------------- runner


def advance(job: Job, config: Config) -> Job:
    """Posune job co nejdál v rámci časového rozpočtu."""
    deadline = time.monotonic() + BUDGET_SECONDS

    try:
        while job.state not in ("DONE", "FAILED", "NEEDS_REVIEW"):
            if time.monotonic() > deadline:
                job.log_step("Časový limit kroku, pokračuji další invokací")
                save(job)
                _self_invoke(job.id)
                return job

            handler = _HANDLERS.get(job.state)
            if handler is None:
                raise RuntimeError(f"Neznámý stav {job.state}")
            handler(job, config, deadline)
            save(job)

    except Exception as exc:  # noqa: BLE001 — job nesmí spadnout bez stopy
        job.state = "FAILED"
        job.error = f"{type(exc).__name__}: {exc}"
        job.log_step(f"Chyba: {job.error}")
        log.exception("Job %s selhal", job.id)
        save(job)
        notify.notify(f"❌ **Sorare job selhal**\n```\n{job.error}\n{traceback.format_exc()[-800:]}\n```")

    return job


def _self_invoke(job_id: str) -> None:
    """Zavolá vlastní endpoint, aby job pokračoval v nové invokaci.

    Fire-and-forget: krátký timeout, chybu ignorujeme. Když se to nepovede,
    job zůstane v Store rozpracovaný a další cron/refresh ho posune dál.
    """
    base = os.environ.get("APP_BASE_URL") or (
        f"https://{os.environ['VERCEL_URL']}" if os.environ.get("VERCEL_URL") else None
    )
    if not base:
        log.warning("APP_BASE_URL není nastaveno, nemůžu navázat — job zůstal rozpracovaný.")
        return
    try:
        requests.post(
            f"{base.rstrip('/')}/api/continue",
            json={"job_id": job_id},
            headers={"X-Internal-Secret": os.environ.get("INTERNAL_SECRET", "")},
            timeout=2,
        )
    except requests.RequestException:
        pass  # očekávané — nečekáme na odpověď


# --------------------------------------------------------------------- steps


def _step_queued(job: Job, config: Config, deadline: float) -> None:
    client = SorareClient(config)
    fixture = client.fetch_open_fixture()
    if not fixture:
        raise SorareError("Nenašel jsem otevřený fixture — nejspíš jsme mezi gameweeky.")
    job.fixture = fixture
    job.log_step(f"Fixture: {fixture.get('displayName') or fixture.get('slug')}")
    job.state = "CARDS"


def _step_cards(job: Job, config: Config, deadline: float) -> None:
    client = SorareClient(config)
    cards = client.fetch_cards()
    job.pending_player_slugs = sorted({c.player.slug for c in cards if c.player.slug})
    job.log_step(f"Portfolio: {len(cards)} karet, {len(job.pending_player_slugs)} hráčů")
    job.state = "SCORES"


def _step_scores(job: Job, config: Config, deadline: float) -> None:
    client = SorareClient(config)
    while job.pending_player_slugs and time.monotonic() < deadline:
        batch = job.pending_player_slugs[:SCORE_BATCH]
        job.scores.update(client.fetch_scores_batch(batch))
        job.pending_player_slugs = job.pending_player_slugs[SCORE_BATCH:]
        save(job)

    if not job.pending_player_slugs:
        job.log_step(f"Skóre stažena pro {len(job.scores)} hráčů")
        job.state = "MLB"


def _step_mlb(job: Job, config: Config, deadline: float) -> None:
    start, end = mlb.gameweek_range()
    games = mlb.schedule(start, end)
    team_ids = sorted(
        {g[s]["id"] for g in games for s in ("home", "away") if g[s].get("id")}
    )
    job.mlb_context = {
        "games": games,
        "name_index": mlb.build_name_index(team_ids),
        "injured": sorted(mlb.injured_player_ids(team_ids)),
    }
    job.log_step(f"MLB: {len(games)} zápasů, {len(job.mlb_context['injured'])} hráčů na IL")
    job.state = "OPTIMIZE"


def _step_optimize(job: Job, config: Config, deadline: float) -> None:
    cards, projections = _rebuild(job, config)
    client = SorareClient(config)
    tournaments = _tournaments(job, config, client)
    if not tournaments:
        job.state = "DONE"
        job.log_step("Žádný turnaj k obsazení — sestavy už jsou nejspíš odeslané.")
        return

    optimizer = LineupOptimizer(config, cards, projections)
    try:
        lineups = optimizer.solve(tournaments, time_limit=20)
    except OptimizationError as exc:
        raise RuntimeError(str(exc)) from exc

    job.lineups = [lu.to_dict() for lu in lineups]
    job.log_step(f"Sestaveno {len(lineups)} sestav")
    job.state = "VALIDATE"


def _step_validate(job: Job, config: Config, deadline: float) -> None:
    cards, projections = _rebuild(job, config)
    ctx = job.mlb_context
    validator = LineupValidator(
        config, cards, projections, ctx["games"], ctx["name_index"], set(ctx["injured"])
    )
    lineups = [_lineup_from_dict(d) for d in job.lineups]
    issues = validator.validate(lineups)
    job.issues = [
        {
            "lineup": i.lineup, "slot": i.slot, "player": i.player,
            "severity": i.severity, "message": i.message,
            "suggested_replacement": i.suggested_replacement,
        }
        for i in issues
    ]

    blockers = [i for i in issues if i.severity == "blocker"]
    job.log_step(f"Kontrola: {len(blockers)} blokujících, {len(issues) - len(blockers)} varování")

    if blockers:
        # Blokující nález = nesestavujeme naslepo. Radši nic než nula bodů.
        job.state = "NEEDS_REVIEW"
        notify.notify(
            notify.format_lineups(lineups, "⚠️ Sestavy NEODESLÁNY — vyžadují zásah")
            + "\n\n"
            + notify.format_issues(issues)
        )
        return

    job.state = "SUBMIT" if job.mode == "auto" else "NEEDS_REVIEW"
    if job.state == "NEEDS_REVIEW":
        notify.notify(
            notify.format_lineups(lineups, "📋 Návrh sestav čeká na tvoje potvrzení")
        )


def _step_submit(job: Job, config: Config, deadline: float) -> None:
    client = SorareClient(config)
    lineups = [_lineup_from_dict(d) for d in job.lineups]

    already = {(s["tournament_slug"], s.get("index", 0)) for s in job.submitted if s.get("ok")}
    for lineup in lineups:
        if (lineup.tournament_slug, lineup.index) in already:
            continue
        try:
            result = client.submit_lineup(lineup.tournament_slug, lineup.card_slugs)
            job.submitted.append(
                {
                    "tournament_slug": lineup.tournament_slug,
                    "tournament_name": lineup.tournament_name,
                    "index": lineup.index,
                    "ok": True,
                    "lineup_id": (result.get("lineup") or {}).get("id"),
                }
            )
            job.log_step(f"Odesláno: {lineup.tournament_name}")
        except SorareError as exc:
            job.submitted.append(
                {
                    "tournament_slug": lineup.tournament_slug,
                    "tournament_name": lineup.tournament_name,
                    "index": lineup.index,
                    "ok": False,
                    "error": str(exc),
                }
            )
            job.log_step(f"Odeslání selhalo: {lineup.tournament_name} — {exc}")
        save(job)

    ok = [s for s in job.submitted if s.get("ok")]
    failed = [s for s in job.submitted if not s.get("ok")]
    job.state = "DONE"

    header = f"✅ Odesláno {len(ok)} sestav" if not failed else (
        f"⚠️ Odesláno {len(ok)}, selhalo {len(failed)}"
    )
    message = notify.format_lineups(lineups, header)
    if failed:
        message += "\n\n" + "\n".join(f"🔴 {f['tournament_name']}: {f['error']}" for f in failed)
    if job.issues:
        message += "\n\n" + notify.format_issues(
            [SimpleNamespace(**i) for i in job.issues]
        )
    notify.notify(message)
    get_store().set(K_LAST_RESULT, asdict(job), ttl_seconds=7 * 24 * 3600)


_HANDLERS = {
    "QUEUED": _step_queued,
    "CARDS": _step_cards,
    "SCORES": _step_scores,
    "MLB": _step_mlb,
    "OPTIMIZE": _step_optimize,
    "VALIDATE": _step_validate,
    "SUBMIT": _step_submit,
}


# --------------------------------------------------------------------- helpers


def _rebuild(job: Job, config: Config) -> tuple[list[Card], dict[str, Projection]]:
    """Z cache poskládá karty + projekce, aniž by znovu volal Sorare."""
    raw_cards = get_store().get_json(K_CARDS) or []
    cards = [card_from_dict(c) for c in raw_cards]

    ctx = job.mlb_context
    engine = ProjectionEngine(
        config, ctx["games"], ctx["name_index"], set(ctx["injured"])
    )
    projections = {}
    for card in cards:
        entry = job.scores.get(card.player.slug) or {}
        projections[card.slug] = engine.project(
            card, entry.get("scores", []), entry.get("last_game")
        )
    return cards, projections


def _tournaments(job: Job, config: Config, client: SorareClient) -> list[Tournament]:
    available = client.fetch_competitions(job.fixture["slug"])
    out: list[Tournament] = []
    for spec in config.get("tournaments", []):
        needle = str(spec.get("slug_contains", "")).lower()
        match = next((c for c in available if needle in str(c.get("slug", "")).lower()), None)
        if not match:
            job.log_step(f"Turnaj '{spec['name']}' nenalezen — přeskakuji")
            continue
        already = int(match.get("lineupsCount") or 0)
        cap = int(match.get("maxLineups") or spec.get("max_lineups", 1))
        remaining = max(0, min(int(spec.get("max_lineups", 1)), cap - already))
        if remaining == 0:
            job.log_step(f"{spec['name']}: už odesláno, přeskakuji")
            continue
        out.append(
            Tournament(
                slug=match["slug"],
                name=spec["name"],
                weight=float(spec.get("weight", 1.0)),
                risk_mode=spec.get("risk_mode", "upside"),
                require_confirmed_lineup=bool(spec.get("require_confirmed_lineup", False)),
                max_lineups=remaining,
            )
        )
    return out


def _lineup_from_dict(raw: dict) -> Lineup:
    return Lineup(
        tournament_slug=raw["tournament_slug"],
        tournament_name=raw["tournament_name"],
        index=raw.get("index", 0),
        slots=[
            LineupSlot(
                slot=s["slot"],
                card_slug=s["card_slug"],
                player_name=s["player"],
                team=s.get("team"),
                projected=float(s.get("projected", 0)),
            )
            for s in raw["slots"]
        ],
    )
