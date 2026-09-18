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
        # Gameweek má 3–4 dny, takže tým hraje víckrát. Držet jen poslední
        # zápas znamenalo, že se probable pitcher skoro nikdy netrefil.
        self._team_games: dict[int, list[dict]] = {}
        for game in games:
            for side in ("home", "away"):
                tid = game[side].get("id")
                # Pozor na `if tid:` — ID 0 je platné a nula je nepravdivá.
                if tid is not None:
                    self._team_games.setdefault(tid, []).append({**game, "side": side})
        for entries in self._team_games.values():
            entries.sort(key=lambda g: str(g.get("start") or ""))

    # ------------------------------------------------------------------ public

    def project(
        self, card: Card, scores: list[float], last_game: str | None = None
    ) -> Projection:
        from . import mlb

        inactive_days = self._days_since(last_game)
        max_gap = self.config.get_path("safety.max_days_without_game")
        if max_gap and inactive_days is not None and inactive_days > float(max_gap):
            return self._unplayable(
                card, f"nenastoupil {int(inactive_days)} dní (limit {max_gap})"
            )

        mlb_player = self._match_player(card)
        if mlb_player is None:
            return self._unplayable(card, "hráč nenalezen v MLB rosteru")

        if self.config.get_path("safety.exclude_injured", True) and (
            mlb_player["id"] in self.injured_ids
        ):
            return self._unplayable(card, "na IL / restricted list")

        team_games = self._team_games.get(mlb_player.get("team_id")) or []
        if not team_games and self.config.get_path("safety.require_scheduled_game", True):
            return self._unplayable(card, "tým v tomto gameweeku nehraje")

        if self._is_hitter(card):
            return self._project_hitter(card, scores, mlb_player, team_games)
        return self._project_pitcher(card, scores, mlb_player, team_games)

    # ------------------------------------------------------------------ hitters

    def _project_hitter(
        self, card: Card, scores: list[float], player: dict, team_games: list[dict]
    ) -> Projection:
        from . import mlb

        weights = self.config.get_path("projection.hitter", {})
        comp: dict[str, float] = {}
        notes: list[str] = []

        base = self._form_base(scores, weights, comp, notes)

        # Matchup a park se průměrují přes všechny zápasy gameweeku, ne jen
        # přes první. Horší ERA soupeřova SP = lepší pro pálkaře.
        matchups, parks, eras = [], [], []
        for game in team_games:
            opp_side = "away" if game["side"] == "home" else "home"
            opp_pitcher_id = game[opp_side].get("probable_pitcher_id")
            mult = 1.0
            if opp_pitcher_id:
                era = mlb.pitcher_stats(opp_pitcher_id, self.season).get("era")
                if era:
                    # ERA 4.00 je neutrál; každý bod ERA hýbe projekcí o ~6 %.
                    mult = max(0.80, min(1.20, 1.0 + (era - 4.00) * 0.06))
                    eras.append(era)
            matchups.append(mult)
            parks.append(1.0 + (mlb.park_factor(game.get("venue")) - 1.0) * 0.7)

        matchup_mult = sum(matchups) / len(matchups)
        park_mult = sum(parks) / len(parks)
        if eras:
            notes.append(f"soupeřovi SP: ERA Ø {sum(eras) / len(eras):.2f} ({len(eras)}/{len(team_games)} ohlášeno)")
        else:
            notes.append("soupeřovi SP zatím neohlášeni")

        w_matchup = float(weights.get("matchup", 0.0))
        w_park = float(weights.get("ballpark", 0.0))
        per_game = base * (1 + w_matchup * (matchup_mult - 1) + w_park * (park_mult - 1))

        comp["matchup_mult"] = round(matchup_mult, 3)
        comp["park_mult"] = round(park_mult, 3)

        games = float(len(team_games))
        notes.append(f"{len(team_games)} zápasů v GW")
        return self._finalize(card, per_game, games, scores, comp, notes, "hitter")

    # ------------------------------------------------------------------ pitchers

    def _project_pitcher(
        self, card: Card, scores: list[float], player: dict, team_games: list[dict]
    ) -> Projection:
        from . import mlb

        weights = self.config.get_path("projection.pitcher", {})
        comp: dict[str, float] = {}
        notes: list[str] = []
        is_sp = "BASEBALL_STARTING_PITCHER" in card.positions

        # Startující nadhazovač boduje jen v zápase, který skutečně odstartuje.
        game = None
        if is_sp:
            # Zdroje jistoty startu, od nejspolehlivějšího:
            #   1. Sorare probablePitchers (to je odznak "PP" na kartě),
            #   2. MLB StatsAPI probable pitcher (chodí až den dva předem).
            mlb_announced = 0
            start_count = 0
            for candidate in team_games:
                probable = candidate[candidate["side"]].get("probable_pitcher_id")
                if probable:
                    mlb_announced += 1
                if probable == player["id"]:
                    start_count += 1
                    game = game or candidate

            starts = None
            if game is not None:
                starts = True
                notes.append(
                    "ohlášený start (MLB)" if start_count == 1
                    else f"ohlášené {start_count} starty (MLB)"
                )
            elif card.sorare_probable_starter is True:
                starts = True
                notes.append("ohlášený start (Sorare PP)")
            elif card.sorare_probable_starter is False:
                starts = False
            elif mlb_announced:
                starts = False

            if game is None:
                game = team_games[0]

            if starts is False:
                mult = float(
                    self.config.get_path("safety.unprojected_pitcher_multiplier", 0.05)
                )
                if mult <= 0:
                    return self._unplayable(card, "nemá ohlášený start v tomto gameweeku")
                # Nevyřazujeme úplně: když nezbude žádný ohlášený nadhazovač,
                # je pořád lepší mít SP než neodeslat sestavu.
                notes.append("bez ohlášeného startu — jen nouzovka")
                comp["no_start_mult"] = mult
            elif starts is None:
                notes.append("start zatím neohlášen")
        else:
            game = team_games[0]

        base = self._form_base(scores, weights, comp, notes)
        base *= comp.get("no_start_mult", 1.0)

        # Kolik vystoupení čekáme. SP: počet ohlášených startů (aspoň 1,
        # když startuje). RP: zápasy týmu × pravděpodobnost nasazení.
        if is_sp:
            games = float(max(start_count, 1)) if starts else 1.0
            role = "starting_pitcher"
        else:
            rate = float(self.config.get_path("projection.rp_appearance_rate", 0.45))
            games = max(1.0, len(team_games) * rate)
            role = "relief_pitcher"
            notes.append(f"{len(team_games)} zápasů týmu, čekaná vystoupení {games:.1f}")

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

        per_game = base * (
            1
            + float(weights.get("k_rate", 0)) * (k_mult - 1)
            + float(weights.get("opp_offense", 0)) * (opp_mult - 1)
            + float(weights.get("home_field", 0)) * (home_mult - 1)
        )
        comp.update(
            {"k_mult": round(k_mult, 3), "opp_mult": round(opp_mult, 3), "home_mult": home_mult}
        )
        # Bez ohlášeného startu nevěříme ani Sorare projekci — ta by penalizaci přebila.
        blend = comp.get("no_start_mult") is None
        return self._finalize(card, per_game, games, scores, comp, notes, role, blend)

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
        self,
        card: Card,
        per_game: float,
        games: float,
        scores: list[float],
        comp: dict,
        notes: list[str],
        role: str,
        blend_sorare: bool = True,
    ) -> Projection:
        """Z projekce na zápas udělá projekci gameweeku, floor a ceiling.

        * Součet přes zápasy: `projection.sum_over_games` (Sorare MLB sčítá
          body ze všech zápasů gameweeku; když ne, nastav false).
        * Rozptyl: vlastní σ z L15 se stahuje k typické σ pozice (shrinkage),
          protože 15 hodnot je na odhad rozptylu málo.
        * Sorare projekce (`nextClassicFixtureProjectedScore`) se přimíchá
          váhou `projection.sorare_weight`.
        * Bonus karty (power) násobí všechno.
        """
        cfg = self.config
        sum_games = bool(cfg.get_path("projection.sum_over_games", True))
        mult_games = games if sum_games else 1.0

        prior = float(cfg.get_path(f"projection.sigma_prior.{role}", {
            "hitter": 9.0, "starting_pitcher": 14.0, "relief_pitcher": 7.0,
        }[role]))
        window = scores[:15]
        n = len(window)
        own = statistics.pstdev(window) if n >= 3 else prior
        w = n / (n + 10)
        sigma_game = w * own + (1 - w) * prior

        mean = per_game * mult_games
        # Rozptyl součtu nezávislých zápasů roste s odmocninou počtu.
        sigma = sigma_game * (mult_games ** 0.5)

        sorare = card.sorare_projection
        weight = float(cfg.get_path("projection.sorare_weight", 0.35))
        if blend_sorare and sorare and sorare > 0 and weight > 0:
            comp["own_mean"] = round(mean, 2)
            comp["sorare_proj"] = round(sorare, 2)
            mean = (1 - weight) * mean + weight * sorare
            notes.append(f"Sorare projekce {sorare:.1f}")

        if card.power and card.power != 1.0:
            mean *= card.power
            sigma *= card.power
            comp["power"] = round(card.power, 3)
            notes.append(f"bonus karty +{(card.power - 1) * 100:.0f} %")

        comp["games"] = round(games, 2)
        comp["sigma"] = round(sigma, 2)
        return Projection(
            card_slug=card.slug,
            mean=round(mean, 2),
            floor=round(max(0.0, mean - 0.85 * sigma), 2),
            ceiling=round(mean + 1.1 * sigma, 2),
            components=comp,
            notes=notes,
            games=round(games, 2),
            sigma=round(sigma, 2),
        )

    @staticmethod
    def _days_since(iso_date: str | None) -> float | None:
        """Kolik dní uplynulo od posledního odehraného zápasu."""
        if not iso_date:
            return None
        try:
            played = datetime.fromisoformat(str(iso_date).replace("Z", "+00:00"))
        except ValueError:
            return None
        now = datetime.now(tz=played.tzinfo) if played.tzinfo else datetime.now()
        return (now - played).total_seconds() / 86400

    def _is_hitter(self, card: Card) -> bool:
        return any(p in self.hitter_positions for p in card.positions)

    def _match_player(self, card: Card) -> dict | None:
        from . import mlb

        return mlb.match_player(self.name_index, card.player.slug, card.player.name)

    @staticmethod
    def _unplayable(card: Card, reason: str) -> Projection:
        return Projection(
            card_slug=card.slug,
            mean=0.0,
            floor=0.0,
            ceiling=0.0,
            playable=False,
            reason_unplayable=reason,
            games=0.0,
        )
