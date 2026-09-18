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

from . import history, mlb, notify
from .client import SorareClient, SorareError, card_from_dict
from .models import Card, Config, Lineup, LineupSlot, Projection, Tournament
from .optimizer import LineupOptimizer, OptimizationError, win_probability
from .projections import ProjectionEngine
from .store import K_CARDS, K_JOB, K_JOB_LOCK, K_LAST_RESULT, K_LATEST_JOB, get_store
from .validator import LineupValidator

log = logging.getLogger(__name__)

# Kolik sekund necháme jako rezervu, než se funkce sama ukončí a předá štafetu.
BUDGET_SECONDS = float(os.environ.get("STEP_BUDGET_SECONDS", 40))
SCORE_BATCH = 25
TERMINAL = ("DONE", "FAILED", "NEEDS_REVIEW")


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
    leaderboards: list[dict] = field(default_factory=list)
    # Slugy hráčů, které Sorare čeká jako startující nadhazovače tohoto GW.
    probable_starters: list[str] = field(default_factory=list)
    # Vrátil dotaz na startéry použitelná data? Když ne, nepenalizujeme.
    starters_known: bool = False
    # Přepsat už odeslané sestavy (druhý běh těsně před uzávěrkou).
    overwrite: bool = False
    # Kdo job spustil: manual | cron | tick-build | tick-recheck
    trigger: str = "manual"
    # Strukturovaný log pro UI: {t, level, text}; level info|warn|error|detail
    events: list[dict] = field(default_factory=list)
    # Náhradníci pro každý slot: "{lineup_pos}:{slot}" -> [{card_slug, ...}]
    alternatives: dict = field(default_factory=dict)

    def log_step(self, text: str, level: str | None = None) -> None:
        now = datetime.now()
        self.steps.append(f"{now:%H:%M:%S} {text}")
        self.events.append({
            "t": now.isoformat(timespec="seconds"),
            "level": level or _guess_level(text),
            "text": text.strip(),
        })
        self.updated_at = now.isoformat(timespec="seconds")


def _guess_level(text: str) -> str:
    low = text.lower()
    if low.startswith(("chyba", "❌")) or "selhal" in low:
        return "error"
    if "varování" in low or "nenalezen" in low or "vynecháno" in low:
        return "warn"
    if text.startswith("  "):
        return "detail"
    return "info"


# --------------------------------------------------------------------- storage


def save(job: Job) -> None:
    store = get_store()
    store.set(K_JOB.format(job_id=job.id), asdict(job), ttl_seconds=6 * 3600)
    store.set(K_LATEST_JOB, job.id, ttl_seconds=6 * 3600)


def load(job_id: str) -> Job | None:
    data = get_store().get_json(K_JOB.format(job_id=job_id))
    if not data:
        return None
    # Joby uložené starší verzí můžou mít jiná pole.
    known = set(Job.__dataclass_fields__)
    return Job(**{k: v for k, v in data.items() if k in known})


def latest_job() -> Job | None:
    job_id = get_store().get(K_LATEST_JOB)
    return load(job_id) if job_id else None


def create_job(mode: str = "auto", overwrite: bool = False, trigger: str = "manual") -> Job:
    job = Job(
        id=uuid.uuid4().hex[:12],
        mode=mode,
        created_at=datetime.now().isoformat(timespec="seconds"),
        overwrite=overwrite,
        trigger=trigger,
    )
    job.log_step(
        f"Job vytvořen (režim: {mode}, spouštěč: {trigger}"
        + (", přepisuje odeslané" if overwrite else "") + ")"
    )
    save(job)
    return job


# --------------------------------------------------------------------- runner


def advance(job: Job, config: Config) -> Job:
    """Posune job co nejdál v rámci časového rozpočtu.

    Zámek v Redisu brání tomu, aby job posouvaly dvě invokace naráz
    (cron + /api/continue). Bez něj šlo v kroku SUBMIT odeslat dvakrát.
    """
    store = get_store()
    lock_key = K_JOB_LOCK.format(job_id=job.id)
    if not store.acquire(lock_key, int(BUDGET_SECONDS) + 20):
        log.info("Job %s právě běží jinde, přeskakuji", job.id)
        return job

    # Stav mohl mezitím posunout někdo jiný — pracujeme s čerstvou verzí.
    fresh = load(job.id)
    if fresh is not None:
        job = fresh

    deadline = time.monotonic() + BUDGET_SECONDS
    started_in = job.state
    handoff = False

    try:
        while job.state not in TERMINAL:
            if time.monotonic() > deadline:
                job.log_step("Časový limit kroku, pokračuji další invokací")
                save(job)
                handoff = True
                break

            handler = _HANDLERS.get(job.state)
            if handler is None:
                raise RuntimeError(f"Neznámý stav {job.state}")
            handler(job, config, deadline)
            save(job)

    except Exception as exc:  # noqa: BLE001 — job nesmí spadnout bez stopy
        job.state = "FAILED"
        job.error = f"{type(exc).__name__}: {exc}"
        job.log_step(f"Chyba: {job.error}", "error")
        log.exception("Job %s selhal", job.id)
        save(job)
        notify.notify(f"❌ **Sorare job selhal**\n```\n{job.error}\n{traceback.format_exc()[-800:]}\n```")
    finally:
        # Zámek pustit dřív, než zavoláme další invokaci — jinak by si ho
        # nová invokace nestihla vzít a job by zůstal viset.
        try:
            store.delete(lock_key)
        except Exception:  # noqa: BLE001
            pass

    if handoff:
        _self_invoke(job.id)
        return job

    # Do historie jen jednou — při přechodu do koncového stavu.
    if job.state in TERMINAL and started_in not in TERMINAL:
        history.record_job(job)

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
    boards = client.fetch_leaderboards()
    if not boards:
        raise SorareError(
            "Žádný otevřený baseballový leaderboard — nejspíš jsme mezi gameweeky."
        )
    fixture, boards = nearest_fixture(boards)
    job.leaderboards = boards
    job.fixture = fixture
    job.log_step(
        f"Gameweek {fixture.get('gameWeek', '?')} ({fixture.get('slug', '?')}): "
        f"{len(boards)} otevřených leaderboardů"
    )
    job.state = "CARDS"


def nearest_fixture(boards: list[dict]) -> tuple[dict, list[dict]]:
    """Fixture s nejbližší uzávěrkou a jen jeho leaderboardy.

    `upcomingLeaderboards` umí vrátit dva gameweeky naráz (typicky přes
    víkend). Brát `boards[0]` míchalo okno zápasů, startéry i leaderboardy
    dvou různých týdnů.
    """
    def cutoff(board: dict) -> str:
        return str(board.get("cutOffDate") or "9999")

    first = min(boards, key=cutoff)
    fixture = first.get("so5Fixture") or {}
    slug = fixture.get("slug")
    same = [b for b in boards if (b.get("so5Fixture") or {}).get("slug") == slug] if slug else boards
    return fixture, same


def _step_cards(job: Job, config: Config, deadline: float) -> None:
    client = SorareClient(config)
    # Před uzávěrkou chceme čerstvá skóre, ne dvouhodinovou cache.
    cards = [c for c in client.fetch_cards(use_cache=False) if not c.in_vault]
    # Skóre chodí ve stejné odpovědi jako karty, takže samostatný krok odpadá.
    job.scores = client.fetch_scores(cards)
    job.pending_player_slugs = []
    job.log_step(f"Portfolio: {len(cards)} karet, {len(job.scores)} hráčů se skóre")

    # Kdo v tomhle gameweeku startuje. Ptáme se zápasů fixture — lavička
    # bez kontextu sestavy vrací prázdno a `nextGame` míří mimo gameweek.
    fixture_slug = (job.fixture or {}).get("slug")
    if fixture_slug:
        starters, error = client.fetch_probable_starters(fixture_slug)
        if starters:
            job.probable_starters = sorted(starters)
            job.starters_known = True
            job.log_step(f"Ohlášení startéři podle Sorare: {len(starters)}")
        else:
            job.log_step(
                f"VAROVÁNÍ: startéry se zjistit nepovedlo ({error}) — "
                "nadhazovači se vybírají jen podle formy a MLB probables"
            )
    else:
        job.log_step("VAROVÁNÍ: neznám slug fixture, startéry nezjistím")

    job.state = "MLB"


def _step_scores(job: Job, config: Config, deadline: float) -> None:
    # Skóre se stahují spolu s kartami; tenhle stav zůstává jen proto, aby
    # joby rozpracované starší verzí nezůstaly viset.
    job.state = "MLB"


def _step_mlb(job: Job, config: Config, deadline: float) -> None:
    # Okno musí vycházet z fixture, ne z dnešního data: gameweek začíná
    # v budoucnu a podle kalendáře bychom počítali zápasy jiného týdne.
    start, end = _fixture_window(job)
    job.log_step(f"Okno gameweeku: {start} – {end}")
    games = mlb.schedule(start, end)
    team_ids = sorted(
        {
            g[s]["id"] for g in games for s in ("home", "away")
            if g[s].get("id") is not None
        }
    )
    job.mlb_context = {
        "games": games,
        "name_index": mlb.build_name_index(team_ids),
        "injured": sorted(mlb.injured_player_ids(team_ids)),
    }
    job.log_step(f"MLB: {len(games)} zápasů, {len(job.mlb_context['injured'])} hráčů na IL")
    job.log_step(
        f"MLB rosterů: {len(job.mlb_context['name_index'])} jmen, "
        f"{len(team_ids)} týmů"
    )
    job.state = "OPTIMIZE"


def _step_optimize(job: Job, config: Config, deadline: float) -> None:
    cards, projections = _rebuild(job, config)
    client = SorareClient(config)
    tournaments = _tournaments(job, config, client)
    if not tournaments:
        job.state = "DONE"
        job.log_step("Žádný turnaj k obsazení — sestavy už jsou nejspíš odeslané.")
        return

    # Rozpad důvodů, proč karty vypadly — bez něj se infeasibilita ladí naslepo.
    from collections import Counter

    reasons = Counter(
        p.reason_unplayable or "?" for p in projections.values() if not p.playable
    )
    playable = sum(1 for p in projections.values() if p.playable)
    job.log_step(f"Použitelných karet: {playable} / {len(projections)}")
    for reason, count in reasons.most_common(5):
        job.log_step(f"  vyřazeno {count}× — {reason}")

    # Ukázka konkrétních karet pomáhá poznat, jestli selhalo párování jmen.
    sample = [
        f"{c.player.name} ({c.player.team_name})"
        for c in cards[:3]
    ]
    job.log_step("Vzorek karet: " + ", ".join(sample))

    optimizer = LineupOptimizer(config, cards, projections)
    try:
        lineups = optimizer.solve(tournaments, time_limit=20)
    except OptimizationError as exc:
        raise RuntimeError(str(exc)) from exc

    targets = {t.slug: t.target_score for t in tournaments}
    job.lineups = []
    for lu in lineups:
        data = lu.to_dict()
        target = targets.get(lu.tournament_slug)
        data["target_score"] = target
        data["win_probability"] = win_probability(lu, target)
        data["sigma"] = round(sum((s.sigma or 0) ** 2 for s in lu.slots) ** 0.5, 2)
        job.lineups.append(data)
    job.alternatives = build_alternatives(job.lineups, cards, projections, config, tournaments)
    job.log_step(f"Sestaveno {len(lineups)} sestav")

    # Nadhazovači jsou nejčastější zdroj překvapení — vypíšeme, proč byli vybráni.
    for lineup in lineups:
        for slot in lineup.slots:
            if slot.slot != "SP":
                continue
            notes = projections[slot.card_slug].notes
            job.log_step(f"  {lineup.tournament_name} #{lineup.index + 1} SP "
                         f"{slot.player_name}: {'; '.join(notes) or '—'}")
    for skipped in optimizer.skipped:
        job.log_step(f"  vynecháno: {skipped} — nedostatek použitelných karet")
    job.state = "VALIDATE"


def _step_validate(job: Job, config: Config, deadline: float, notify_user: bool = True) -> None:
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
        if not notify_user:
            return
        notify.notify(
            notify.format_lineups(lineups, "⚠️ Sestavy NEODESLÁNY — vyžadují zásah")
            + "\n\n"
            + notify.format_issues(issues)
        )
        return

    job.state = "SUBMIT" if job.mode == "auto" else "NEEDS_REVIEW"
    if job.state == "NEEDS_REVIEW" and notify_user:
        notify.notify(
            notify.format_lineups(lineups, "📋 Návrh sestav čeká na tvoje potvrzení")
        )


def _step_submit(job: Job, config: Config, deadline: float) -> None:
    client = SorareClient(config)
    lineups = [_lineup_from_dict(d) for d in job.lineups]
    cards, _ = _rebuild(job, config)
    boards_by_slug = {b["slug"]: b["id"] for b in (job.leaderboards or [])}
    # Každá sestava v jednom leaderboardu potřebuje vlastní manager team;
    # Sorare jinak hlásí "This manager team already has a lineup".
    teams_by_slug = {
        b["slug"]: [t["id"] for t in (b.get("myManagerTeams") or []) if t.get("id")]
        for b in (job.leaderboards or [])
    }
    requires_team = {
        b["slug"]: bool(b.get("requiresManagerTeam"))
        for b in (job.leaderboards or [])
    }

    # Sorare mapuje appearances na sloty podle pořadí, takže kontrolujeme
    # obojí: že karta na pozici slotu sedí, i v jakém pořadí se posílá.
    slots_cfg = config.get_path("lineup.slots", {})
    by_slug = {c.slug: c for c in cards}
    for lineup in lineups:
        popis = []
        for i, slot in enumerate(lineup.slots):
            card = by_slug.get(slot.card_slug)
            positions = card.positions if card else []
            allowed = slots_cfg.get(slot.slot, [])
            ok = bool(set(positions) & set(allowed))
            popis.append(
                f"{i}:{slot.slot}={slot.player_name}"
                f"[{','.join(p.replace('BASEBALL_', '') for p in positions)}]"
                + ("" if ok else " !NESEDÍ")
            )
        job.log_step(f"  {lineup.tournament_name} #{lineup.index + 1}: " + " | ".join(popis))

    # Přepis existujících sestav (recheck před uzávěrkou) potřebuje jejich ID.
    existing: dict[str, list[str]] = {}
    id_field = None
    if job.overwrite:
        try:
            from .features import get_features

            id_field = (get_features().get("lineup_input") or {}).get("id_field")
            existing = client.existing_lineup_ids() if id_field else {}
        except Exception as exc:  # noqa: BLE001
            job.log_step(f"VAROVÁNÍ: existující sestavy nezjištěny ({exc})")
        if not id_field:
            job.log_step("VAROVÁNÍ: schéma neumí ID sestavy v mutaci — přepis vypnut, "
                         "odešlou se jen chybějící sestavy")

    already = {(s["tournament_slug"], s.get("index", 0)) for s in job.submitted if s.get("ok")}
    for lineup in lineups:
        if (lineup.tournament_slug, lineup.index) in already:
            continue
        try:
            board_id = boards_by_slug.get(lineup.tournament_slug)
            if not board_id:
                raise SorareError(
                    f"Neznám ID leaderboardu pro {lineup.tournament_slug}."
                )
            teams = teams_by_slug.get(lineup.tournament_slug) or []
            # index sestavy = index týmu; když tým chybí, necháme ho založit
            team_id = teams[lineup.index] if lineup.index < len(teams) else None
            ids = existing.get(lineup.tournament_slug) or []
            lineup_id = ids[lineup.index] if lineup.index < len(ids) else None
            if job.overwrite and not id_field:
                board = next((b for b in job.leaderboards if b["slug"] == lineup.tournament_slug), {})
                if lineup.index < int(board.get("mySo5LineupsCount") or 0):
                    job.log_step(f"Přeskočeno (už odesláno, přepis nejde): {lineup.tournament_name} #{lineup.index + 1}")
                    continue
            result = client.submit_lineup(
                board_id,
                lineup.card_slugs,
                manager_team_id=team_id,
                requires_manager_team=requires_team.get(lineup.tournament_slug, False),
                lineup_id=lineup_id,
                lineup_id_field=id_field,
            )
            job.submitted.append(
                {
                    "tournament_slug": lineup.tournament_slug,
                    "tournament_name": lineup.tournament_name,
                    "index": lineup.index,
                    "ok": True,
                    "lineup_id": (result.get("so5Lineup") or {}).get("id"),
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
    try:
        from . import calibration

        calibration.record(job)
    except Exception:  # noqa: BLE001 — kalibrace nesmí shodit odeslání
        log.exception("Uložení pro kalibraci selhalo")


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
    # Karty v trezoru nejsou k dispozici pro sestavy.
    cards = [c for c in (card_from_dict(r) for r in raw_cards) if not c.in_vault]

    if job.starters_known:
        starters = set(job.probable_starters)
        for card in cards:
            card.sorare_probable_starter = card.player.slug in starters

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


def build_alternatives(
    lineups: list[dict],
    cards: list[Card],
    projections: dict[str, Projection],
    config: Config,
    tournaments: list[Tournament] | None = None,
    limit: int = 3,
) -> dict:
    """Nejlepší nepoužité karty pro každý slot — podklad pro prohození v UI."""
    slots_cfg = config.get_path("lineup.slots", {})
    used = {s["card_slug"] for lu in lineups for s in lu["slots"]}
    rarities = {t.slug: {r.lower() for r in t.allowed_rarities} for t in (tournaments or [])}
    out: dict[str, list[dict]] = {}
    for pos, lu in enumerate(lineups):
        players_in = {s.get("player_slug") for s in lu["slots"]}
        allowed_rar = rarities.get(lu["tournament_slug"]) or set()
        for slot in lu["slots"]:
            allowed = set(slots_cfg.get(slot["slot"], []))
            current = projections.get(slot["card_slug"])
            base = current.mean if current else 0.0
            cands = [
                c for c in cards
                if c.slug not in used
                and set(c.positions) & allowed
                and (not allowed_rar or c.rarity in allowed_rar)
                and c.player.slug not in players_in
                and projections.get(c.slug) and projections[c.slug].playable
            ]
            cands.sort(key=lambda c: projections[c.slug].mean, reverse=True)
            out[f"{pos}:{slot['slot']}"] = [
                {
                    "card_slug": c.slug,
                    "player": c.player.name,
                    "team": c.player.team_name,
                    "projected": projections[c.slug].mean,
                    "delta": round(projections[c.slug].mean - base, 2),
                    "in_season": c.in_season,
                }
                for c in cands[:limit]
            ]
    return out


def swap_card(job: Job, config: Config, pos: int, slot_name: str, card_slug: str) -> Job:
    """Ruční prohození karty v navržené sestavě + nová kontrola."""
    if job.state != "NEEDS_REVIEW":
        raise ValueError(f"Prohazovat jde jen ve stavu NEEDS_REVIEW (teď {job.state}).")
    if not 0 <= pos < len(job.lineups):
        raise ValueError("Neznámá sestava.")
    cards, projections = _rebuild(job, config)
    by_slug = {c.slug: c for c in cards}
    card = by_slug.get(card_slug)
    proj = projections.get(card_slug)
    if card is None or proj is None or not proj.playable:
        raise ValueError("Karta není k dispozici nebo není použitelná.")
    allowed = set(config.get_path(f"lineup.slots.{slot_name}", []))
    if not set(card.positions) & allowed:
        raise ValueError(f"Karta nesedí do slotu {slot_name}.")
    used = {s["card_slug"] for lu in job.lineups for s in lu["slots"]}
    if card_slug in used:
        raise ValueError("Karta už je v jiné sestavě.")

    lineup = job.lineups[pos]
    others = [s for s in lineup["slots"] if s["slot"] != slot_name]
    if card.player.slug in {s.get("player_slug") for s in others}:
        raise ValueError("Tenhle hráč už v sestavě je.")
    spec = next(
        (t for t in config.get("tournaments", []) if t.get("name") == lineup["tournament_name"]),
        {},
    )
    rarity = str(spec.get("rarity", "")).lower()
    if rarity and card.rarity != rarity:
        raise ValueError(f"Turnaj bere jen raritu {rarity}.")
    min_in = spec.get("min_in_season")
    if min_in is not None:
        fresh = sum(1 for s in others if by_slug.get(s["card_slug"]) and by_slug[s["card_slug"]].in_season)
        if fresh + (1 if card.in_season else 0) < int(min_in):
            raise ValueError(f"Sestava by neměla {min_in} karet se season bonusem.")

    for s in lineup["slots"]:
        if s["slot"] != slot_name:
            continue
        old = s["player"]
        s.update({
            "card_slug": card.slug, "player": card.player.name, "team": card.player.team_name,
            "projected": proj.mean, "player_slug": card.player.slug, "floor": proj.floor,
            "ceiling": proj.ceiling, "sigma": proj.sigma, "games": proj.games, "notes": list(proj.notes),
        })
        job.log_step(f"Ruční záměna {lineup['tournament_name']} #{lineup['index'] + 1} "
                     f"{slot_name}: {old} → {card.player.name}")
        break

    lu_obj = _lineup_from_dict(lineup)
    lineup["total_projected"] = round(lu_obj.total_projected, 2)
    lineup["win_probability"] = win_probability(lu_obj, lineup.get("target_score"))
    lineup["sigma"] = round(sum((s.sigma or 0) ** 2 for s in lu_obj.slots) ** 0.5, 2)

    # Nová kontrola a nové alternativy.
    _step_validate(job, config, time.monotonic() + 30, notify_user=False)
    job.state = "NEEDS_REVIEW"
    job.alternatives = build_alternatives(job.lineups, cards, projections, config)
    save(job)
    return job


def _tournaments(job: Job, config: Config, client: SorareClient) -> list[Tournament]:
    """Spáruje leaderboardy ze Sorare s turnaji z configu."""
    available = job.leaderboards or client.fetch_leaderboards()
    out: list[Tournament] = []

    for spec in config.get("tournaments", []):
        needle = str(spec.get("slug_contains", "")).lower()
        rarity = str(spec.get("rarity", "")).lower()
        matches = [
            b for b in available
            if needle in str(b.get("slug", "")).lower()
            and (not rarity or str(b.get("rarityType", "")).lower() == rarity)
        ]
        if not matches:
            job.log_step(f"Turnaj '{spec['name']}' nenalezen — přeskakuji")
            continue

        for board in matches:
            wanted = int(spec.get("max_lineups", 1))
            if job.overwrite or config.get_path("submission.overwrite_existing", False):
                # createOrUpdate existující sestavu přepíše, takže se nemusíme
                # ohlížet na to, kolik jich už je.
                remaining = wanted
            else:
                already = int(board.get("mySo5LineupsCount") or 0)
                remaining = max(0, wanted - already)
                if remaining == 0:
                    job.log_step(f"{spec['name']} ({board['slug']}): už odesláno")
                    continue

            out.append(
                Tournament(
                    slug=board["slug"],
                    name=spec["name"],
                    weight=float(spec.get("weight", 1.0)),
                    risk_mode=spec.get("risk_mode", "upside"),
                    require_confirmed_lineup=bool(spec.get("require_confirmed_lineup", False)),
                    max_lineups=remaining,
                    leaderboard_id=board["id"],
                    min_in_season=spec.get("min_in_season"),
                    # Bez tohohle by rare karta skončila v limited leaderboardu
                    # a Sorare by sestavu odmítl.
                    allowed_rarities=[rarity] if rarity else [],
                    target_score=_as_float(spec.get("target_score")),
                )
            )
    return out


def _as_float(value) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _fixture_window(job: Job) -> tuple:
    """Rozsah dat gameweeku podle So5 fixture, s fallbackem na kalendář."""
    from datetime import datetime

    fixture = job.fixture or {}

    def parse(value):
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
        except ValueError:
            return None

    start = parse(fixture.get("startDate"))
    end = parse(fixture.get("endDate"))
    if start and end:
        return start, end

    job.log_step("Fixture nemá data, používám odhad podle kalendáře")
    return mlb.gameweek_range()


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
                player_slug=s.get("player_slug"),
                floor=s.get("floor"),
                ceiling=s.get("ceiling"),
                sigma=s.get("sigma"),
                games=s.get("games"),
                notes=list(s.get("notes") or []),
            )
            for s in raw["slots"]
        ],
    )
