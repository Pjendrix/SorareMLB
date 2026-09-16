"""Přehledy a historie bez sítě."""
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sorare_mlb import history, overview  # noqa: E402
from sorare_mlb.client import SorareError, card_from_dict  # noqa: E402
from sorare_mlb.models import Config  # noqa: E402
from sorare_mlb.sports import FootballAdapter, NotSupported, get_adapter  # noqa: E402
from sorare_mlb.store import LocalStore  # noqa: E402

TODAY = date(2026, 9, 16)


def node(name, pos, scores, last_days_ago, rarity="limited"):
    last = (TODAY - timedelta(days=last_days_ago)).isoformat() if last_days_ago is not None else None
    return {
        "slug": f"{name}-card", "rarityTyped": rarity, "seasonYear": 2026,
        "anyPositions": [pos], "anyTeam": {"slug": "t", "name": "Team"},
        "anyPlayer": {
            "slug": name, "displayName": name.title(),
            "playerGameScores": [{"score": s, "anyGame": {"date": last}} for s in scores],
        },
    }


@pytest.fixture
def store(monkeypatch, tmp_path):
    import sorare_mlb.client as client_mod
    import sorare_mlb.store as store_mod

    monkeypatch.setattr(store_mod, "LOCAL_FILE", tmp_path / "s.json")
    fake = LocalStore()
    for mod in (store_mod, client_mod, history, overview):
        monkeypatch.setattr(mod, "get_store", lambda: fake)
    return fake


def test_summary_flags_form_and_idle():
    cards = [
        card_from_dict(node("hot", "FORWARD", [90, 80, 85, 70, 75, 20, 20, 20, 20, 20], 2)),
        card_from_dict(node("cold", "DEFENDER", [10, 10, 10, 10, 10, 60, 60, 60, 60, 60], 3)),
        card_from_dict(node("bench", "GOALKEEPER", [40], 40, rarity="rare")),
        card_from_dict(node("never", "MIDFIELDER", [], None)),
    ]
    s = FootballAdapter().summarize(cards, Config({}), today=TODAY)
    assert s["count"] == 4
    assert s["by_rarity"] == {"limited": 3, "rare": 1}
    assert s["in_form"][0]["player"] == "Hot"
    assert s["cooling"][0]["player"] == "Cold"
    assert {r["player"] for r in s["idle"]} == {"Bench", "Never"}
    assert s["by_position"]["Forward"] == 1


def test_snapshot_once_per_day(store):
    summary = {"count": 3, "avg_l15": 40.0, "idle_count": 1, "by_rarity": {}}
    assert history.snapshot_if_due("football", summary, TODAY)
    assert not history.snapshot_if_due("football", summary, TODAY)
    assert history.snapshot_if_due("football", summary, TODAY + timedelta(days=1))
    snaps = history.snapshots("football")
    assert [s["date"] for s in snaps] == ["2026-09-16", "2026-09-17"]


def test_job_history_is_capped(store, monkeypatch):
    monkeypatch.setattr(history, "MAX_JOBS", 3)
    from sorare_mlb.runner import Job

    for i in range(5):
        history.record_job(Job(id=str(i), state="DONE", lineups=[
            {"tournament_name": "Hot Streaks", "index": 0, "total_projected": 100 + i, "slots": []}
        ]))
    jobs = history.jobs()
    assert [j["job_id"] for j in jobs] == ["4", "3", "2"]
    assert jobs[0]["lineups"][0]["projected"] == 104


class FakeClient:
    calls = []

    def __init__(self, config):
        pass

    def fetch_all_upcoming(self, cache_seconds=0):
        cut = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
        return [
            {"slug": "gw1-champion_pve-limited", "displayName": "Hot Streak", "rarityType": "limited",
             "cutOffDate": cut, "mySo5LineupsCount": 0,
             "so5Fixture": {"slug": "b1", "gameWeek": 1, "sport": "BASEBALL"}},
            {"slug": "gw9-cap", "displayName": "Cap", "rarityType": "limited",
             "cutOffDate": cut, "mySo5LineupsCount": 2,
             "so5Fixture": {"slug": "f9", "gameWeek": 9, "sport": "FOOTBALL"}},
        ]

    def fetch_recent_lineups(self, cache_seconds=600):
        return {"variant": "RecentLineupsFixture", "lineups": [
            {"id": "1", "so5Leaderboard": {"displayName": "Cap", "so5Fixture": {
                "sport": "FOOTBALL", "gameWeek": 9, "startDate": "2020-01-01", "endDate": "2020-01-02"}}},
        ]}


def test_upcoming_groups_and_tracks(monkeypatch):
    monkeypatch.setattr(overview, "SorareClient", FakeClient)
    config = Config({"tournaments": [{"slug_contains": "champion_pve", "rarity": "limited"}]})
    up = overview.upcoming(config)
    assert up["boards"]["mlb"][0]["tracked"] is True
    assert up["boards"]["football"][0]["mine"] == 2
    assert {f["sport"] for f in up["fixtures"]} == {"mlb", "football"}


def test_recent_lineups_without_scores(monkeypatch):
    monkeypatch.setattr(overview, "SorareClient", FakeClient)
    rec = overview.recent_lineups(Config({}))
    assert rec["has_sport"] and not rec["has_scores"]
    assert rec["lineups"][0]["sport"] == "football"


def test_execute_first_ok_falls_back():
    from sorare_mlb.client import SorareClient

    client = SorareClient.__new__(SorareClient)
    tried = []

    def execute(query, variables=None, operation_name=None, retries=3, tolerate_errors=False):
        tried.append(operation_name)
        if operation_name == "Rich":
            raise SorareError("Field 'score' doesn't exist")
        return {"data": {"ok": True}}

    client.execute = execute
    data, variant = client.execute_first_ok([("Rich", "q"), ("Basic", "q")])
    assert variant == "Basic" and data == {"ok": True} and tried == ["Rich", "Basic"]


def test_football_lineups_not_supported():
    with pytest.raises(NotSupported):
        get_adapter("football").build_lineups()
