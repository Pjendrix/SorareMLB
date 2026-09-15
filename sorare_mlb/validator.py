"""Kontrola sestav před uzávěrkou.

Na oficiální MLB lineupy se spolehnout nedá — zveřejňují se pozdě, nepravidelně
a u ranních běhů vůbec. Blokující kritéria jsou proto ta, která jsou k dispozici
vždycky:

* hráč je na IL nebo restricted listu,
* jeho tým v tomhle gameweeku nehraje,
* dlouho nenastoupil (řeší se už v projekcích, tady se jen revaliduje).

Oficiální lineup se použije, když náhodou k dispozici je — ale jen jako
varování, nikdy jako důvod sestavu zablokovat.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import mlb
from .models import Card, Config, Lineup, Projection


@dataclass
class Issue:
    lineup: str
    slot: str
    player: str
    severity: str        # "blocker" | "warning"
    message: str
    suggested_replacement: str | None = None


class LineupValidator:
    def __init__(
        self,
        config: Config,
        cards: list[Card],
        projections: dict[str, Projection],
        games: list[dict],
        name_index: dict[str, dict],
        injured_ids: set[int],
    ):
        self.config = config
        self.cards = {c.slug: c for c in cards}
        self.projections = projections
        self.games = games
        self.name_index = name_index
        self.injured_ids = injured_ids
        self.slots = config.get_path("lineup.slots", {})

    def validate(self, lineups: list[Lineup]) -> list[Issue]:
        used = {s.card_slug for lu in lineups for s in lu.slots}
        issues: list[Issue] = []

        # Cache oficiálních lineupů po zápase, ať netaháme feed opakovaně.
        lineup_cache: dict[int, dict[str, list[int]]] = {}

        for lineup in lineups:
            for slot in lineup.slots:
                card = self.cards.get(slot.card_slug)
                if card is None:
                    continue
                player = self.name_index.get(mlb.normalize_name(card.player.name))

                if player is None:
                    issues.append(
                        Issue(lineup.tournament_name, slot.slot, slot.player_name,
                              "warning", "hráč nenapárován na MLB roster")
                    )
                    continue

                if player["id"] in self.injured_ids:
                    issues.append(
                        Issue(lineup.tournament_name, slot.slot, slot.player_name, "blocker",
                              "je na IL",
                              self._suggest(card, slot.slot, used))
                    )
                    continue

                projection = self.projections.get(slot.card_slug)
                if projection is not None and not projection.playable:
                    issues.append(
                        Issue(lineup.tournament_name, slot.slot, slot.player_name, "blocker",
                              projection.reason_unplayable or "hráč není použitelný",
                              self._suggest(card, slot.slot, used))
                    )
                    continue

                game = self._game_for(player.get("team_id"))
                if game is None:
                    issues.append(
                        Issue(lineup.tournament_name, slot.slot, slot.player_name, "blocker",
                              "tým dnes nehraje",
                              self._suggest(card, slot.slot, used))
                    )
                    continue

                game_pk = game["game_pk"]
                if game_pk not in lineup_cache:
                    lineup_cache[game_pk] = mlb.confirmed_lineup(game_pk)
                confirmed = lineup_cache[game_pk]

                if not confirmed:
                    # Běžný stav, ne problém — nezahlcujeme tím výpis.
                    continue

                all_ids = {pid for ids in confirmed.values() for pid in ids}
                is_pitcher = "PITCHER" in " ".join(card.positions).upper()
                if not is_pitcher and player["id"] not in all_ids:
                    # Varování, ne blocker: lineup mohl být zveřejněn jen částečně
                    # a zbytečné blokování by zabránilo odeslání úplně.
                    issues.append(
                        Issue(lineup.tournament_name, slot.slot, slot.player_name, "warning",
                              "není v právě zveřejněné základní sestavě",
                              self._suggest(card, slot.slot, used))
                    )

        return issues

    # ------------------------------------------------------------------ helpers

    def _game_for(self, team_id: int | None) -> dict | None:
        if team_id is None:
            return None
        for game in self.games:
            if game["home"].get("id") == team_id or game["away"].get("id") == team_id:
                return game
        return None

    def _suggest(self, card: Card, slot: str, used: set[str]) -> str | None:
        """Nejlepší nepoužitá karta, která sedí do stejného slotu."""
        allowed = set(self.slots.get(slot, []))
        candidates = [
            c
            for c in self.cards.values()
            if c.slug not in used
            and set(c.positions) & allowed
            and self.projections.get(c.slug)
            and self.projections[c.slug].playable
        ]
        if not candidates:
            return None
        best = max(candidates, key=lambda c: self.projections[c.slug].mean)
        return f"{best.player.name} ({best.slug})"
