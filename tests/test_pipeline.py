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


FRESH = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
STALE = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()


def card_node(slug, pos, tag, i, pi, last_game=None, rarity="limited"):
    """Uzel karty ve tvaru, v jakém ho vrací dotaz USER_CARDS."""
    day = last_game or FRESH
    return {
        "slug": slug, "rarityTyped": rarity, "seasonYear": datetime.now().year,
        "anyPositions": [f"BASEBALL_{pos.upper()}"],
        "anyTeam": {"slug": f"team{(i + pi) % 6}", "name": f"Team {(i + pi) % 6}"},
        "anyPlayer": {
            # Pozor: normalize_name zahazuje číslice, takže jména musí být
            # rozlišitelná písmeny, ne indexy.
            "slug": f"p-{slug}", "displayName": f"Player {tag}{chr(97 + i)}",
            "playerGameScores": [
                {"score": sc, "anyGame": {"date": day}}
                for sc in (45.0, 50.0, 40.0, 55.0, 38.0, 47.0)
            ],
        },
    }


def boards():
    cutoff = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    fixture = {"slug": "fixture-1", "gameWeek": 1, "sport": "BASEBALL",
               "startDate": "2026-09-14T00:00:00Z", "endDate": "2026-09-17T00:00:00Z"}
    later = {**fixture, "slug": "fixture-2", "gameWeek": 2}
    late_cut = (datetime.now(timezone.utc) + timedelta(days=4)).isoformat()
    return [
        {"id": "hs-id", "slug": "mlb-champion_pve-limited", "rarityType": "limited",
         "cutOffDate": cutoff, "mySo5LineupsCount": 0, "requiresManagerTeam": False,
         "myManagerTeams": [], "so5Fixture": fixture},
        {"id": "ch-id", "slug": "mlb-challenger-limited", "rarityType": "limited",
         "cutOffDate": cutoff, "mySo5LineupsCount": 0, "requiresManagerTeam": True,
         "myManagerTeams": [], "so5Fixture": fixture},
        # Příští gameweek — nesmí se smíchat s tím aktuálním.
        {"id": "hs2-id", "slug": "mlb-champion_pve-limited-2", "rarityType": "limited",
         "cutOffDate": late_cut, "mySo5LineupsCount": 0, "so5Fixture": later},
    ]


@pytest.fixture
def fake_world(monkeypatch, store):
    cards = []
    for pi, (pos, tag) in enumerate(POSITIONS):
        for i in range(6):  # 4 sestavy × 7 slotů = 28 míst, pool musí být větší
            cards.append(card_node(f"{tag}{i}", pos, tag, i, pi))
    submitted = []

    from sorare_mlb.client import SorareClient as Real

    class FakeClient:
        nodes = cards

        def __init__(self, config, token=None):
            pass

        def fetch_leaderboards(self, sport="BASEBALL"):
            return boards()

        def fetch_cards(self, use_cache=True):
            from sorare_mlb.client import card_from_dict
            store.set(K_CARDS, self.nodes)
            return [card_from_dict(c) for c in self.nodes]

        fetch_scores = Real.fetch_scores

        def fetch_probable_starters(self, fixture_slug):
            assert fixture_slug == "fixture-1"
            return set(), "test: nikdo neohlášen"

        def existing_lineup_ids(self):
            return {}

        def submit_lineup(self, board_id, card_slugs, **kwargs):
            assert board_id in ("hs-id", "ch-id"), "odesláno do špatného gameweeku"
            assert len(card_slugs) == 7
            submitted.append(board_id)
            return {"so5Lineup": {"id": f"lineup-{board_id}-{len(submitted)}"}}

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
    monkeypatch.setattr(runner, "_self_invoke", lambda job_id: None)
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
    """Hráč bez zápasu déle než limit se do sestavy nedostane."""
    stale_cards = []
    for pi, (pos, tag) in enumerate(POSITIONS):
        for i in range(6):
            # Hráči „a“ a „b“ měsíc nenastoupili.
            stale_cards.append(card_node(f"{tag}{i}", pos, tag, i, pi,
                                         last_game=STALE if i < 2 else FRESH))
    monkeypatch.setattr(runner.SorareClient, "nodes", stale_cards)

    job = runner.create_job(mode="propose")
    job = runner.advance(job, config)

    assert job.state == "NEEDS_REVIEW", f"{job.state}: {job.error}"
    picked = {s["card_slug"] for lu in job.lineups for s in lu["slots"]}
    assert picked, "nevybrala se žádná karta"
    assert not any(slug.endswith(("0", "1")) for slug in picked), (
        "vybrán hráč, který měsíc nenastoupil"
    )


def test_only_nearest_fixture_is_used(fake_world, config, store):
    job = runner.create_job(mode="propose")
    job = runner.advance(job, config)
    assert job.fixture["slug"] == "fixture-1"
    assert {b["slug"] for b in job.leaderboards} == {
        "mlb-champion_pve-limited", "mlb-challenger-limited",
    }


def test_rare_cards_stay_out_of_limited(fake_world, config, store, monkeypatch):
    nodes = list(runner.SorareClient.nodes)
    # Nejlepší SP je rare — do limited turnaje nesmí.
    rare = card_node("rare-sp", "starting_pitcher", "rsp", 0, 0, rarity="rare")
    rare["anyPlayer"]["playerGameScores"] = [
        {"score": 99.0, "anyGame": {"date": FRESH}} for _ in range(6)
    ]
    rare["anyPlayer"]["displayName"] = "Player sp" + "a"  # existuje v MLB indexu
    rare["anyPlayer"]["slug"] = "p-rare"
    monkeypatch.setattr(runner.SorareClient, "nodes", nodes + [rare])
    job = runner.advance(runner.create_job(mode="propose"), config)
    picked = {s["card_slug"] for lu in job.lineups for s in lu["slots"]}
    assert "rare-sp" not in picked


def test_lock_prevents_parallel_advance(fake_world, config, store):
    from sorare_mlb.store import K_JOB_LOCK

    job = runner.create_job(mode="propose")
    assert store.acquire(K_JOB_LOCK.format(job_id=job.id), 60)
    after = runner.advance(job, config)
    assert after.state == "QUEUED", "job se posunul i přes cizí zámek"


def test_swap_card_revalidates(fake_world, config, store):
    job = runner.advance(runner.create_job(mode="propose"), config)
    key, alts = next((k, v) for k, v in job.alternatives.items() if v)
    pos, slot = key.split(":")
    target = alts[0]["card_slug"]
    job = runner.swap_card(job, config, int(pos), slot, target)
    assert any(s["card_slug"] == target for s in job.lineups[int(pos)]["slots"])
    assert job.state == "NEEDS_REVIEW"
