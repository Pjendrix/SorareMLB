"""Výpočet projekce bodů pro každou kartu.

Filozofie: základ je forma (Sorare L15/L5), protože ta v sobě už nese roli
hráče i kvalitu. MLB StatsAPI slouží ke **korekcím** — matchup, ballpark,
domácí prostředí — a hlavně k tvrdému vyřazení hráčů, kteří nehrají.

Floor a ceiling se odvozují ze směrodatné odchylky posledních skóre. Hot Streaks
optimalizuje floor, Challenger ceiling.
"""
from __future__ import annotations

import statistics
from datetime import datetime

from .models import Card, Config, Projection


class ProjectionEngine:
    def __init__(
        self,
        config: Config,
        games: list[dict],
        name_index: dict[str, dict],
        injured_ids: set[int],
        season: int | None = None,
    ):
        self.config = config
        self.games = games
        self.name_index = name_index
        self.injured_ids = injured_ids
        self.season = season or datetime.now().year
        self.hitter_positions = set(config.get_path("lineup.hitter_positions", []))
        self._team_game: dict[int, dict] = {}
        for game in games:
            for side in ("home", "away"):
                tid = game[side].get("id")
                if tid:
                    self._team_game[tid] = {**game, "side": side}

    # ------------------------------------------------------------------ public

    def project(self, card: Card, scores: list[float]) -> Projection:
        from . import mlb

        mlb_player = self._match_player(card)
        if mlb_player is None:
            return self._unplayable(card, "hráč nenalezen v MLB rosteru")

        if self.config.get_path("safety.exclude_injured", True) and (
            mlb_player["id"] in self.injured_ids
        ):
            return self._unplayable(card, "na IL / restricted list")

        game = self._team_game.get(mlb_player.get("team_id"))
        if game is None and self.config.get_path("safety.require_scheduled_game", True):
            return self._unplayable(card, "tým v tomto gameweeku nehraje")

        is_pitcher = not self._is_hitter(card)
        if is_pitcher:
            proj = self._project_pitcher(card, scores, mlb_player, game)
        else:
            proj = self._project_hitter(card, scores, mlb_player, game)
        return proj

    # ------------------------------------------------------------------ hitters

    def _project_hitter(self, card: Card, scores: list[float], player: dict, game: dict) -> Projection:
        from . import mlb

        weights = self.config.get_path("projection.hitter", {})
        comp: dict[str, float] = {}
        notes: list[str] = []

        base = self._form_base(scores, weights, comp, notes)

        # Matchup: soupeřův probable pitcher. Horší ERA soupeře = lepší pro pálkaře.
        opp_side = "away" if game["side"] == "home" else "home"
        opp_pitcher_id = game[opp_side].get("probable_pitcher_id")
        matchup_mult = 1.0
        if opp_pitcher_id:
            stats = mlb.pitcher_stats(opp_pitcher_id, self.season)
            era = stats.get("era")
            if era:
                # ERA 4.00 je neutrál; každý bod ERA hýbe projekcí o ~6 %.
                matchup_mult = 1.0 + (era - 4.00) * 0.06
                matchup_mult = max(0.80, min(1.20, matchup_mult))
                notes.append(f"soupeř SP ERA {era:.2f}")
        else:
            notes.append("soupeřův SP zatím neohlášen")

        park_mult = 1.0 + (mlb.park_factor(game.get("venue")) - 1.0) * 0.7

        w_matchup = float(weights.get("matchup", 0.0))
        w_park = float(weights.get("ballpark", 0.0))
        mean = base * (1 + w_matchup * (matchup_mult - 1) + w_park * (park_mult - 1))

        comp["matchup_mult"] = round(matchup_mult, 3)
        comp["park_mult"] = round(park_mult, 3)

        return self._finalize(card, mean, scores, comp, notes)

    # ------------------------------------------------------------------ pitchers

    def _project_pitcher(self, card: Card, scores: list[float], player: dict, game: dict) -> Projection:
        from . import mlb

        weights = self.config.get_path("projection.pitcher", {})
        comp: dict[str, float] = {}
        notes: list[str] = []

        base = self._form_base(scores, weights, comp, notes)

        is_sp = "starting_pitcher" in card.positions
        if is_sp:
            probable_id = game[game["side"]].get("probable_pitcher_id")
            if probable_id and probable_id != player["id"]:
                return self._unplayable(card, "není ohlášený probable pitcher tohoto GW")
            if not probable_id:
                notes.append("probable pitcher zatím neohlášen — projekce nejistá")

        stats = mlb.pitcher_stats(player["id"], self.season)
        k_rate = stats.get("k_rate")
        k_mult = 1.0
        if k_rate:
            # 22 % K je zhruba ligový průměr.
            k_mult = max(0.82, min(1.25, 1.0 + (k_rate - 0.22) * 2.2))
            notes.append(f"K% {k_rate*100:.1f}")

        opp_side = "away" if game["side"] == "home" else "home"
        opp_id = game[opp_side].get("id")
        opp_mult = 1.0
        if opp_id:
            ops = (mlb.team_offense(opp_id, self.season) or {}).get("ops")
            if ops:
                # Vyšší OPS soupeře = horší pro nadhazovače.
                opp_mult = max(0.82, min(1.18, 1.0 - (ops - 0.720) * 0.9))
                notes.append(f"soupeř OPS {ops:.3f}")

        home_mult = 1.03 if game["side"] == "home" else 0.98

        mean = base * (
            1
            + float(weights.get("k_rate", 0)) * (k_mult - 1)
            + float(weights.get("opp_offense", 0)) * (opp_mult - 1)
            + float(weights.get("home_field", 0)) * (home_mult - 1)
        )
        comp.update(
            {"k_mult": round(k_mult, 3), "opp_mult": round(opp_mult, 3), "home_mult": home_mult}
        )
        return self._finalize(card, mean, scores, comp, notes)

    # ------------------------------------------------------------------ helpers

    def _form_base(
        self, scores: list[float], weights: dict, comp: dict, notes: list[str]
    ) -> float:
        cold = float(self.config.get_path("projection.cold_start_score", 20.0))
        min_games = int(self.config.get_path("projection.min_games_for_trust", 5))

        if len(scores) < min_games:
            notes.append(f"jen {len(scores)} odehraných skóre — použit cold-start odhad")
            comp["base"] = cold
            return cold

        l15 = statistics.fmean(scores[:15])
        l5 = statistics.fmean(scores[:5])
        season = statistics.fmean(scores)

        w15 = float(weights.get("l15_avg", 0.4))
        w5 = float(weights.get("l5_avg", 0.3))
        ws = float(weights.get("season_avg", 0.1))
        total = w15 + w5 + ws
        base = (l15 * w15 + l5 * w5 + season * ws) / total if total else l15

        comp.update({"l15": round(l15, 2), "l5": round(l5, 2), "base": round(base, 2)})
        return base

    def _finalize(
        self, card: Card, mean: float, scores: list[float], comp: dict, notes: list[str]
    ) -> Projection:
        spread = statistics.pstdev(scores[:15]) if len(scores) >= 3 else mean * 0.45
        return Projection(
            card_slug=card.slug,
            mean=round(mean, 2),
            floor=round(max(0.0, mean - 0.85 * spread), 2),
            ceiling=round(mean + 1.1 * spread, 2),
            components=comp,
            notes=notes,
        )

    def _is_hitter(self, card: Card) -> bool:
        return any(p in self.hitter_positions for p in card.positions)

    def _match_player(self, card: Card) -> dict | None:
        from . import mlb

        manual = mlb.manual_player_map.get(card.player.slug)
        if manual:
            for entry in self.name_index.values():
                if entry["id"] == manual:
                    return entry
        return self.name_index.get(mlb.normalize_name(card.player.name))

    @staticmethod
    def _unplayable(card: Card, reason: str) -> Projection:
        return Projection(
            card_slug=card.slug,
            mean=0.0,
            floor=0.0,
            ceiling=0.0,
            playable=False,
            reason_unplayable=reason,
        )
