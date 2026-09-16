"""Sporty jako vyměnitelné adaptéry.

Pipeline i přehledy mluví se sportem jen přes `SportAdapter`. Přidat builder
sestav pro další sport tak znamená doplnit metody adaptéru, ne sahat do UI
ani do runneru.
"""
from .base import SportAdapter, NotSupported
from .baseball import BaseballAdapter
from .football import FootballAdapter

ADAPTERS: dict[str, type[SportAdapter]] = {
    "mlb": BaseballAdapter,
    "football": FootballAdapter,
}


def get_adapter(key: str) -> SportAdapter:
    try:
        return ADAPTERS[key]()
    except KeyError as exc:
        raise KeyError(f"Neznámý sport: {key}") from exc


__all__ = ["ADAPTERS", "SportAdapter", "NotSupported", "get_adapter"]
