"""Fotbal — zatím jen čtení (sbírka, turnaje, historie).

Skládání sestav je připravené jako rozhraní: až vznikne optimalizátor,
doplní se `build_lineups`, `validate_lineups` a `submit_lineups` a přepne
se `sports.football.lineups_enabled` v config.yaml.
"""
from __future__ import annotations

from .base import SportAdapter


class FootballAdapter(SportAdapter):
    key = "football"
    sport = "FOOTBALL"
    label = "Fotbal"
    lineups_supported = False

    # Plánovaná sestava, ať placeholder v UI ukazuje reálný formát.
    PLANNED_SLOTS = ["Brankář", "Obránce", "Záložník", "Útočník", "Extra"]
