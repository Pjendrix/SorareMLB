"""Scheduler, kalibrace, bonus karty a projekce přes více zápasů."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sorare_mlb import scheduler  # noqa: E402
from sorare_mlb.client import _power_mult, card_from_dict  # noqa: E402
from sorare_mlb.models import Config  # noqa: E402
from sorare_mlb.projections import ProjectionEngine  # noqa: E402
from sorare_mlb.store import LocalStore  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def config():
    return Config.load(ROOT / "config.yaml")


@pytest.fixture
def store(monkeypatch, tmp_path):
    import sorare_mlb.store as store_mod

    monkeypatch.setattr(store_mod, "LOCAL_FILE", tmp_path / "store.json")
    fake = LocalStore()
    monkeypatch.setattr(store_mod, "_store", fake)
    monkeypatch.setattr(store_mod, "get_store", lambda: fake)
    import sorare_mlb.calibration as cal
    monkeypatch.setattr(cal, "get_store", lambda: fake)
    return fake


# ------------------------------------------------------------------ scheduler


def _board(minutes, slug="fx"):
    now = datetime(2026, 9, 18, 20, 0, tzinfo=timezone.utc)
    return now, {
        "slug": f"b-{slug}", "cutOffDate": (now + timedelta(minutes=minutes)).isoformat(),
        "so5Fixture": {"slug": slug, "gameWeek": 1},
    }


@pytest.mark.parametrize("minutes,action", [
    (300, "idle"), (55, "build"), (15, "recheck"), (2, "idle"),
])
def test_plan_windows(config, minutes, action):
    now, board = _board(minutes)
    assert scheduler.plan(config, [board], now=now)["action"] == action


def test_plan_picks_nearest_fixture(config):
    now, near = _board(40, "near")
    _, far = _board(3000, "far")
    assert scheduler.plan(config, [far, near], now=now)["fixture"] == "near"


def test_lock_is_exclusive(store):
    assert store.acquire("k", 60)
    assert not store.acquire("k", 60)
    store.delete("k")
    assert store.acquire("k", 60)


# ------------------------------------------------------------------ bonus karty


@pytest.mark.parametrize("raw,expected", [
    (None, 1.0), ("1.05", 1.05), (0.05, 1.05), ("5%", 1.05), ("abc", 1.0), (7, 1.0),
])
def test_power_parsing(raw, expected):
    assert _power_mult(raw) == pytest.approx(expected)


# ------------------------------------------------------------------ projekce


def _hitter(power=None, sorare=None):
    node = {
        "slug": "c1", "rarityTyped": "limited", "seasonYear": 2026,
        "anyPositions": ["BASEBALL_OUTFIELD"], "anyTeam": {"slug": "t", "name": "T"},
        "cardPower": power,
        "anyPlayer": {"slug": "p1", "displayName": "John Doe",
                      "nextClassicFixtureProjectedScore": sorare,
                      "playerGameScores": [{"score": 20.0, "anyGame": {"date": "2026-09-17"}}] * 10},
    }
    return card_from_dict(node)


def _engine(config, n_games):
    games = [{
        "game_pk": g, "start": f"2026-09-1{g}T23:00:00Z", "venue": "X",
        "home": {"id": 1, "probable_pitcher_id": None},
        "away": {"id": 2, "probable_pitcher_id": None},
    } for g in range(n_games)]
    index = {"john doe": {"id": 7, "name": "John Doe", "team_id": 1}}
    return ProjectionEngine(config, games, index, set(), season=2026)


def _cfg(config, **proj):
    data = dict(config)
    data["projection"] = {**config["projection"], **proj}
    data["safety"] = {**config["safety"], "max_days_without_game": 0}
    return Config(data)


def test_more_games_more_points(config):
    cfg = _cfg(config, sorare_weight=0)
    card = _hitter()
    two = _engine(cfg, 2).project(card, card.recent_scores)
    four = _engine(cfg, 4).project(card, card.recent_scores)
    assert four.mean == pytest.approx(2 * two.mean, rel=0.01)
    assert four.games == 4


def test_power_and_sorare_blend(config):
    cfg = _cfg(config, sorare_weight=0.5, sum_over_games=False)
    base = _engine(cfg, 1).project(_hitter(), [20.0] * 10).mean
    boosted = _engine(cfg, 1).project(_hitter(power="1.10"), [20.0] * 10).mean
    blended = _engine(cfg, 1).project(_hitter(sorare=40.0), [20.0] * 10).mean
    assert boosted == pytest.approx(base * 1.10, rel=0.01)
    assert blended == pytest.approx((base + 40.0) / 2, rel=0.01)


# ------------------------------------------------------------------ kalibrace


def test_calibration_roundtrip(config, store):
    from types import SimpleNamespace

    from sorare_mlb import calibration
    from sorare_mlb.store import K_CARDS

    job = SimpleNamespace(
        fixture={"slug": "fx", "gameWeek": 5, "startDate": "2026-09-01T00:00:00Z",
                 "endDate": "2026-09-03T00:00:00Z"},
        submitted=[{"tournament_slug": "hs", "index": 0, "ok": True, "lineup_id": "L"}],
        lineups=[{"tournament_slug": "hs", "tournament_name": "Hot Streaks", "index": 0,
                  "target_score": 100,
                  "slots": [{"slot": "OF", "player": "A", "player_slug": "pa", "card_slug": "ca",
                             "projected": 30.0, "floor": 10, "ceiling": 50}]}],
    )
    assert calibration.record(job) == 1
    store.set(K_CARDS, [{"slug": "ca", "anyPlayer": {"slug": "pa", "playerGameScores": [
        {"score": 10.0, "anyGame": {"date": "2026-09-01"}},
        {"score": 15.0, "anyGame": {"date": "2026-09-02"}},
        {"score": 99.0, "anyGame": {"date": "2026-08-20"}},  # mimo gameweek
    ]}}])
    assert calibration.evaluate(config) == 1
    summary = calibration.summary()
    assert summary["points"][0]["actual"] == 25.0
    assert summary["stats"]["bias"] == -5.0
    assert summary["lineups"][0]["hit"] is False
