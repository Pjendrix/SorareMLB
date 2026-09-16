"""MLB — jediný sport, pro který umíme sestavy skládat i odesílat."""
from __future__ import annotations

from ..client import SorareClient
from ..models import Card, Config
from .base import SportAdapter


class BaseballAdapter(SportAdapter):
    key = "mlb"
    sport = "BASEBALL"
    label = "MLB"
    lineups_supported = True

    def fetch_cards(self, client: SorareClient, config: Config) -> tuple[list[Card], bool]:
        # Sdílí cache s pipeline, takže přehled nestahuje portfolio podruhé.
        return client.fetch_cards(use_cache=True), False

    def build_lineups(self, mode: str = "propose"):
        """Skládání běží přes stavový automat v runneru."""
        from .. import runner

        return runner.create_job(mode=mode)
