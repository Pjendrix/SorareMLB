"""Testy optimalizátoru bez síťových volání."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sorare_mlb.models import Card, Config, Player, Projection  # noqa: E402
from sorare_mlb.optimizer import LineupOptimizer, OptimizationError  # noqa: E402

CONFIG = Config(
    {
        "lineup": {
            "slots": {
                "SP": ["starting_pitcher"],
                "RP": ["relief_pitcher"],
                "CI": ["first_base", "third_base", "catcher"],
                "MI": ["second_base", "shortstop"],
                "OF": ["outfield"],
                "EH": ["first_base", "second_base", "outfield", "designated_hitter"],
                "FLEX": ["first_base", "second_base", "outfield", "designated_hitter"],
            },
            "hitter_positions": [
                "first_base", "third_base", "catcher", "second_base",
                "shortstop", "outfield", "designated_hitter",
            ],
        },
        "stack": {"enabled": True, "bonus": 3.0, "max_from_team": 4, "apply_to": ["Hot Streaks"]},
        "safety": {"min_projected_floor": 0},
        "blacklist_cards": [],
    }
)


def make_card(slug, positions, team="nyy", mean=40.0):
    player = Player(slug=f"p-{slug}", name=slug, positions=positions, team_slug=team, team_name=team)
    return Card(slug=slug, rarity="limited", season=2026, player=player)


def build_pool(n_per_position=3):
    positions = [
        ("starting_pitcher", "sp"),
        ("relief_pitcher", "rp"),
        ("first_base", "1b"),
        ("second_base", "2b"),
        ("outfield", "of"),
        ("designated_hitter", "dh"),
        ("third_base", "3b"),
        ("shortstop", "ss"),
    ]
    cards, projections = [], {}
    # Týmy rozprostřeme, ať limit `max_from_team` neblokuje triviální sestavy —
    # v reálném portfoliu je hráčů z jednoho týmu jen pár.
    for pi, (pos, tag) in enumerate(positions):
        for i in range(n_per_position):
            card = make_card(f"{tag}{i}", [pos], team=f"team{(i + pi) % 6}")
            cards.append(card)
            mean = 50.0 - i * 5
            projections[card.slug] = Projection(
                card_slug=card.slug, mean=mean, floor=mean - 10, ceiling=mean + 15
            )
    return cards, projections


def tournament(name, weight, max_lineups=1, risk="safe"):
    from sorare_mlb.models import Tournament

    return Tournament(
        slug=name.lower().replace(" ", "-"),
        name=name,
        weight=weight,
        risk_mode=risk,
        require_confirmed_lineup=risk == "safe",
        max_lineups=max_lineups,
    )


def test_fills_all_slots():
    cards, projections = build_pool()
    opt = LineupOptimizer(CONFIG, cards, projections)
    lineups = opt.solve([tournament("Hot Streaks", 3.0)])
    assert len(lineups) == 1
    assert len(lineups[0].slots) == 7
    assert {s.slot for s in lineups[0].slots} == set(CONFIG["lineup"]["slots"])


def test_card_never_reused_across_tournaments():
    cards, projections = build_pool(n_per_position=4)
    opt = LineupOptimizer(CONFIG, cards, projections)
    lineups = opt.solve(
        [tournament("Hot Streaks", 3.0), tournament("Challenger", 1.0, risk="upside")]
    )
    all_slugs = [s.card_slug for lu in lineups for s in lu.slots]
    assert len(all_slugs) == len(set(all_slugs)), "karta se objevila ve dvou sestavách"


def test_priority_tournament_gets_better_players():
    # Stack bonus by tady mátl měření — záměrně ho vypínáme, ať testujeme
    # jen to, že vyšší váha turnaje přitáhne lepší hráče.
    cards, projections = build_pool(n_per_position=4)
    config = Config({**CONFIG, "stack": {**CONFIG["stack"], "enabled": False}})
    opt = LineupOptimizer(config, cards, projections)
    lineups = opt.solve(
        [tournament("Hot Streaks", 5.0), tournament("Challenger", 1.0, risk="upside")]
    )
    hs = next(lu for lu in lineups if lu.tournament_name == "Hot Streaks")
    ch = next(lu for lu in lineups if lu.tournament_name == "Challenger")
    assert hs.total_projected > ch.total_projected


def test_unplayable_cards_excluded():
    cards, projections = build_pool()
    for slug in ("sp0", "sp1"):
        projections[slug] = Projection(
            card_slug=slug, mean=0, floor=0, ceiling=0,
            playable=False, reason_unplayable="IL",
        )
    opt = LineupOptimizer(CONFIG, cards, projections)
    lineups = opt.solve([tournament("Hot Streaks", 3.0)])
    sp = next(s for s in lineups[0].slots if s.slot == "SP")
    assert sp.card_slug == "sp2"


def test_raises_when_pool_too_small():
    cards, projections = build_pool(n_per_position=1)
    opt = LineupOptimizer(CONFIG, cards, projections)
    with pytest.raises(OptimizationError):
        opt.solve([tournament("Hot Streaks", 3.0), tournament("Challenger", 1.0, max_lineups=3)])


def test_floor_constraint_blocks_risky_cards():
    cards, projections = build_pool()
    config = Config({**CONFIG, "safety": {"min_projected_floor": 45.0}})
    for slug, proj in projections.items():
        projections[slug] = Projection(
            card_slug=slug, mean=proj.mean,
            floor=10.0 if slug.endswith("2") else 50.0,
            ceiling=proj.ceiling,
        )
    opt = LineupOptimizer(config, cards, projections)
    lineups = opt.solve([tournament("Hot Streaks", 3.0)])
    assert all(projections[s.card_slug].floor >= 45.0 for s in lineups[0].slots)
