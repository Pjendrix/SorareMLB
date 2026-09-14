"""Test pipeline bez sítě — Sorare i MLB jsou nahrazené fakes.

Smysl: ověřit, že stavový automat projde celou cestou, že se stav správně
ukládá a načítá mezi kroky (což na Vercelu odpovídá samostatným invokacím),
a že blokující nález zastaví automatické odeslání.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sorare_mlb import runner  # noqa: E402
from sorare_mlb.models import Config  # noqa: E402
from sorare_mlb.store import K_CARDS, LocalStore  # noqa: E402

POSITIONS = [
    ("starting_pitcher", "sp"), ("relief_pitcher", "rp"), ("first_base", "fb"),
    ("second_base", "sb"), ("outfield", "of"), ("designated_hitter", "dh"),
    ("third_base", "tb"), ("shortstop", "ss"),
]


@pytest.fixture
def store(monkeypatch, tmp_path):
    import sorare_mlb.store as store_mod

    monkeypatch.setattr(store_mod, "LOCAL_FILE", tmp_path / "store.json")
    fake = LocalStore()
    monkeypatch.setattr(store_mod, "_store", fake)
    monkeypatch.setattr(store_mod, "get_store", lambda: fake)
    monkeypatch.setattr(runner, "get_store", lambda: fake)
    return fake


@pytest.fixture
def fake_world(monkeypatch, store):
    cards, players = [], []
    for pi, (pos, tag) in enumerate(POSITIONS):
        for i in range(6):  # 4 sestavy × 7 slotů = 28 míst, pool musí být větší
            slug = f"{tag}{i}"
            cards.append({
                "slug": slug, "rarity": "limited", "seasonYear": 2026,
                "player": {
                    # Pozor: normalize_name zahazuje číslice, takže jména
                    # musí být rozlišitelná písmeny, ne indexy.
                    "slug": f"p-{slug}", "displayName": f"Player {tag}{chr(97 + i)}",
                    "positions": [pos],
                    "team": {"slug": f"team{(i + pi) % 6}", "abbreviation": f"T{(i+pi)%6}"},
                },
            })
            players.append(f"p-{slug}")
    store.set(K_CARDS, cards)

    # --- fake Sorare client ---
    class FakeClient:
        def __init__(self, config, token=None):
            pass

        def fetch_open_fixture(self):
            return {"slug": "fixture-1", "displayName": "GW 1", "state": "opened"}

        def fetch_cards(self, use_cache=True):
            from sorare_mlb.client import card_from_dict
            return [card_from_dict(c) for c in cards]

        def fetch_scores_batch(self, slugs):
            recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
            return {
                s: {"scores": [45.0, 50.0, 40.0, 55.0, 38.0, 47.0], "last_game": recent}
                for s in slugs
            }

        def fetch_competitions(self, fixture_slug):
            return [
                {"slug": "mlb-hot-streak-limited", "lineupsCount": 0, "maxLineups": 1},
                {"slug": "mlb-challenger-limited", "lineupsCount": 0, "maxLineups": 3},
            ]

        def submit_lineup(self, competition_slug, card_slugs):
            assert len(card_slugs) == 7
            return {"lineup": {"id": f"lineup-{competition_slug}"}}

    monkeypatch.setattr(runner, "SorareClient", FakeClient)

    # --- fake MLB ---
    import sorare_mlb.mlb as mlb_mod

    name_index, games = {}, []
    for t in range(6):
        games.append({
            "game_pk": 1000 + t, "start": "2026-09-14T23:10:00Z", "status": "Preview",
            "venue": "Dodger Stadium",
            "home": {"id": t, "name": f"Team {t}", "abbrev": f"T{t}",
                     "probable_pitcher_id": None, "probable_pitcher": None},
            "away": {"id": 100 + t, "name": f"Opp {t}", "abbrev": f"O{t}",
                     "probable_pitcher_id": None, "probable_pitcher": None},
        })
    for pi, (pos, tag) in enumerate(POSITIONS):
        for i in range(6):  # 4 sestavy × 7 slotů = 28 míst, pool musí být větší
            key = mlb_mod.normalize_name(f"Player {tag}{chr(97 + i)}")
            name_index[key] = {
                "id": pi * 10 + i, "name": f"Player {tag}{chr(97 + i)}",
                "position": pos, "status": "Active", "team_id": (i + pi) % 6,
            }

    monkeypatch.setattr(mlb_mod, "schedule", lambda s, e: games)
    monkeypatch.setattr(mlb_mod, "build_name_index", lambda ids: name_index)
    monkeypatch.setattr(mlb_mod, "injured_player_ids", lambda ids: set())
    monkeypatch.setattr(mlb_mod, "pitcher_stats", lambda pid, season: {})
    monkeypatch.setattr(mlb_mod, "team_offense", lambda tid, season: {})
    monkeypatch.setattr(mlb_mod, "confirmed_lineup", lambda pk: {})

    sent = []
    monkeypatch.setattr(runner.notify, "notify", lambda text: sent.append(text) or True)
    return sent


@pytest.fixture
def config():
    return Config.load(Path(__file__).resolve().parents[1] / "config.yaml")


def test_pipeline_reaches_done_in_auto_mode(fake_world, config, store):
    job = runner.create_job(mode="auto")
    job = runner.advance(job, config)
    assert job.state == "DONE", f"{job.state}: {job.error}\n" + "\n".join(job.steps)
    assert len([s for s in job.submitted if s["ok"]]) == 4  # 1× HS + 3× Challenger
    assert any("Odesláno" in m for m in fake_world)


def test_propose_mode_stops_for_review(fake_world, config, store):
    job = runner.create_job(mode="propose")
    job = runner.advance(job, config)
    assert job.state == "NEEDS_REVIEW"
    assert job.submitted == []
    assert job.lineups


def test_state_survives_reload_between_invocations(fake_world, config, store):
    """Každý krok si načteme znovu ze Store — simulace nové funkce na Vercelu."""
    job = runner.create_job(mode="auto")
    guard = 0
    while job.state not in ("DONE", "FAILED", "NEEDS_REVIEW") and guard < 20:
        runner.advance(job, config)
        reloaded = runner.load(job.id)
        assert reloaded is not None, "stav se neuložil"
        job = reloaded
        guard += 1
    assert job.state == "DONE", job.error


def test_injured_player_blocks_auto_submit(fake_world, config, store, monkeypatch):
    import sorare_mlb.mlb as mlb_mod

    # Všichni nadhazovači na IL → sestavu nelze postavit ani odeslat.
    monkeypatch.setattr(mlb_mod, "injured_player_ids", lambda ids: set(range(0, 6)))
    job = runner.create_job(mode="auto")
    job = runner.advance(job, config)
    assert job.state == "FAILED"
    assert job.submitted == []


def test_no_lineup_is_submitted_twice(fake_world, config, store):
    job = runner.create_job(mode="auto")
    job = runner.advance(job, config)
    slugs = [s for lu in job.lineups for s in lu["slots"]]
    card_slugs = [s["card_slug"] for s in slugs]
    assert len(card_slugs) == len(set(card_slugs))


def test_inactive_players_are_excluded(fake_world, config, store, monkeypatch):
    """Hráč bez zápasu déle než limit se do sestavy nedostane.

    Nahrazuje to kontrolu oficiálních MLB lineupů, která je nespolehlivá —
    ty se zveřejňují pozdě a u ranních běhů vůbec.
    """
    stale = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    fresh = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

    original = runner.SorareClient

    class StaleClient(original):
        def fetch_scores_batch(self, slugs):
            out = {}
            for s in slugs:
                # Každý druhý hráč dlouho nenastoupil.
                last = stale if s.endswith(("a", "b")) else fresh
                out[s] = {"scores": [45.0] * 6, "last_game": last}
            return out

    monkeypatch.setattr(runner, "SorareClient", StaleClient)

    job = runner.create_job(mode="propose")
    job = runner.advance(job, config)

    assert job.state == "NEEDS_REVIEW", f"{job.state}: {job.error}"
    picked = {s["card_slug"] for lu in job.lineups for s in lu["slots"]}
    assert picked, "nevybrala se žádná karta"
    assert not any(slug.endswith(("a", "b")) for slug in picked), (
        "vybrán hráč, který měsíc nenastoupil"
    )
