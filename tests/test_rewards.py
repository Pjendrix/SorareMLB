"""Výhry, trezor a dotazy skládané ze schématu."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sorare_mlb import queries, rewards  # noqa: E402
from sorare_mlb.features import build_features  # noqa: E402
from sorare_mlb.schema import Schema  # noqa: E402

SDL = (Path(__file__).parent / "fixtures" / "mini_schema.graphql").read_text()


def test_schema_parser_handles_multiline_args_and_directives():
    s = Schema(SDL)
    assert s.field("CurrentUser", "cards").args == ["sport", "first", "after"]
    assert s.field("CurrentUser", "rewardedRankings").arg_types["sport"] == "[Sport!]"
    assert s.node_type("CurrentUser", "cards") == "AnyCardInterface"
    assert set(s.possible_types("AnyRewardInterface")) == {
        "CardShardsReward", "MonetaryReward", "CoinReward", "AnyCardReward"
    }


def test_built_queries_validate_against_schema():
    graphql = pytest.importorskip("graphql")
    gql_schema = graphql.build_schema(SDL)
    feats = build_features(Schema(SDL))

    assert feats["vault"]["field"] == "inVault"
    assert feats["rewards"]["sport_is_list"] is True
    errors = graphql.validate(gql_schema, graphql.parse(feats["rewards"]["query"]))
    assert not errors, errors

    cards_q = queries.SPORT_CARDS.replace(
        "anyPositions", f"anyPositions {feats['vault']['selection']}"
    )
    # anyPositions/anyTeam v mini schématu nejsou — kontrolujeme jen syntaxi.
    graphql.parse(cards_q)


RAW = {
    "id": "r1",
    "scoreValue": 410.5,
    "rankValue": 120,
    "lineupRef": {
        "boardRef": {"slug": "gw14-all-star", "displayName": "All Star", "rarityType": "limited",
                     "fixtureRef": {"gameWeek": 14, "sport": "FOOTBALL", "endDate": "2026-09-15"}},
        "appearancesRef": [
            {"scoreValue": 75, "cardRef": {"slug": "c1", "rarityTyped": "limited",
                                            "playerRef": {"slug": "jardim", "displayName": "Leo Jardim"}}},
            {"scoreValue": 25, "cardRef": {"slug": "c2", "rarityTyped": "limited",
                                            "playerRef": {"slug": "cucu", "displayName": "Cucurella"}}},
        ],
    },
    "rewardsRef": [{
        "moneyRef": {"eurCents": 0},
        "itemsRef": [
            {"__typename": "CardShardsReward", "CardShardsReward__quantity": 30,
             "CardShardsReward__rarity": "limited",
             "CardShardsReward__player": {"slug": "jardim", "displayName": "Leo Jardim"}},
            {"__typename": "CardShardsReward", "CardShardsReward__quantity": 20,
             "CardShardsReward__rarity": "limited"},
            {"__typename": "MonetaryReward", "MonetaryReward__money": {"eurCents": 400}},
        ],
    }],
}


def test_normalize_and_attribution():
    row = rewards.normalize(RAW)
    assert row["sport"] == "football" and row["game_week"] == 14
    s = rewards.summarize([row])
    assert s["totals"]["money"] == 4.0
    assert s["totals"]["essence"] == 50
    assert s["essence_exact_share"] == 0.6
    cards = {c["player"]: c for c in s["cards"]}
    # 30 přesně Jardimovi, 20 rozpočítaných 75:25, peníze taky 75:25
    assert cards["Leo Jardim"]["essence_total"] == 45
    assert cards["Cucurella"]["essence_est"] == 5
    assert cards["Leo Jardim"]["money"] == 3.0
    assert s["competitions"][0]["best"] == 120


def test_summary_filters_sport_and_rarity():
    row = rewards.normalize(RAW)
    rare = dict(row, id="r2", rarity="rare")
    assert rewards.summarize([row, rare], "football", ["limited"])["count"] == 1
    assert rewards.summarize([row], "mlb")["count"] == 0


def test_archive_merges_by_id(monkeypatch, tmp_path):
    import sorare_mlb.store as store_mod

    monkeypatch.setattr(store_mod, "LOCAL_FILE", tmp_path / "s.json")
    fake = store_mod.LocalStore()
    monkeypatch.setattr(rewards, "get_store", lambda: fake)
    row = rewards.normalize(RAW)
    rewards.merge([row])
    rewards.merge([dict(row, score=500), dict(row, id="r3")])
    rows = rewards.archive("football")
    assert len(rows) == 2 and {r["score"] for r in rows} == {500, 410.5}
