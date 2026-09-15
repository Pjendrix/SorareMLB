"""Datové typy a načtení konfigurace."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml


# --------------------------------------------------------------------------- config


class Config(dict):
    """Tenký wrapper nad YAML configem s tečkovým přístupem."""

    @classmethod
    def load(cls, path: str | Path = "config.yaml") -> "Config":
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(
                f"Nenalezen {path}. Zkopíruj config.yaml z repozitáře."
            )
        with path.open(encoding="utf-8") as fh:
            return cls(yaml.safe_load(fh))

    def get_path(self, dotted: str, default: Any = None) -> Any:
        node: Any = self
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


# --------------------------------------------------------------------------- domain


@dataclass
class Player:
    slug: str
    name: str
    positions: list[str]
    team_slug: str | None = None
    team_name: str | None = None
    status: str | None = None          # z MLB StatsAPI: Active / IL10 / ...
    is_injured: bool = False

    @property
    def is_pitcher(self) -> bool:
        return any(p in ("starting_pitcher", "relief_pitcher") for p in self.positions)


@dataclass
class Card:
    slug: str
    rarity: str
    season: int | None
    player: Player
    # Sorare skóre z minulosti, nejnovější první.
    recent_scores: list[float] = field(default_factory=list)
    # Datum posledního odehraného zápasu (ISO) — vstup pro filtr neaktivity.
    last_game: str | None = None
    # Karta se season bonusem (aktuální sezóna). In-season soutěže jich
    # vyžadují minimální počet.
    in_season: bool = True

    @property
    def positions(self) -> list[str]:
        return self.player.positions

    def avg_last(self, n: int) -> float | None:
        window = [s for s in self.recent_scores[:n] if s is not None]
        return sum(window) / len(window) if window else None


@dataclass
class Game:
    """Jeden reálný MLB zápas v rámci gameweeku."""

    game_pk: int
    start: datetime
    home_team: str
    away_team: str
    venue: str
    home_probable_pitcher: str | None = None
    away_probable_pitcher: str | None = None
    # Naplní se až když MLB zveřejní oficiální sestavu.
    confirmed_lineup: dict[str, list[str]] = field(default_factory=dict)

    def opponent_of(self, team: str) -> str | None:
        if team == self.home_team:
            return self.away_team
        if team == self.away_team:
            return self.home_team
        return None

    def is_home(self, team: str) -> bool:
        return team == self.home_team


@dataclass
class Tournament:
    slug: str
    name: str
    weight: float
    risk_mode: str
    require_confirmed_lineup: bool
    max_lineups: int
    deadline: datetime | None = None
    allowed_rarities: list[str] = field(default_factory=list)
    # Mutace chce ID leaderboardu, ne slug.
    leaderboard_id: str | None = None
    # Kolik karet se season bonusem sestava potřebuje.
    # None = bez omezení (Challenger), 6 = in-season soutěže.
    min_in_season: int | None = None


@dataclass
class Projection:
    card_slug: str
    mean: float
    floor: float
    ceiling: float
    # Rozpad pro `--explain`.
    components: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    playable: bool = True
    reason_unplayable: str | None = None


@dataclass
class LineupSlot:
    slot: str
    card_slug: str
    player_name: str
    team: str | None
    projected: float


@dataclass
class Lineup:
    tournament_slug: str
    tournament_name: str
    index: int
    slots: list[LineupSlot]

    @property
    def total_projected(self) -> float:
        return sum(s.projected for s in self.slots)

    @property
    def card_slugs(self) -> list[str]:
        return [s.card_slug for s in self.slots]

    def to_dict(self) -> dict:
        return {
            "tournament_slug": self.tournament_slug,
            "tournament_name": self.tournament_name,
            "index": self.index,
            "total_projected": round(self.total_projected, 2),
            "slots": [
                {
                    "slot": s.slot,
                    "card_slug": s.card_slug,
                    "player": s.player_name,
                    "team": s.team,
                    "projected": round(s.projected, 2),
                }
                for s in self.slots
            ],
        }


def env(key: str, default: str | None = None, required: bool = False) -> str | None:
    value = os.environ.get(key, default)
    if required and not value:
        raise RuntimeError(f"Chybí proměnná prostředí {key}. Doplň ji do .env")
    return value
