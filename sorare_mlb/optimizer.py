"""Optimalizace sestav přes všechny turnaje najednou.

Model (celočíselné lineární programování, PuLP + CBC):

    x[card, lineup, slot] ∈ {0,1}   karta obsazuje daný slot dané sestavy

Tvrdá omezení:
    1. každý slot každé sestavy obsazen právě jednou,
    2. každá karta použita nejvýš jednou **napříč všemi sestavami**,
    3. karta smí do slotu jen s kompatibilní pozicí,
    4. karta musí být playable (hraje, není na IL),
    5. limit počtu hráčů z jednoho reálného týmu,
    6. minimální floor pro sestavy s `risk_mode: safe`.

Účelová funkce:
    Σ váha_turnaje × hodnota_karty + stack bonus

Proč ne dvě samostatné optimalizace: karta jde použít jen jednou, takže
rozhodnutí v Hot Streaks mění, co zbude na Challenger. Greedy "nejdřív HS"
tuhle vazbu ignoruje a vyrábí lokální optimum.
"""
from __future__ import annotations

from collections import defaultdict

import pulp

from .models import Card, Config, Lineup, LineupSlot, Projection, Tournament


class OptimizationError(RuntimeError):
    pass


class LineupOptimizer:
    def __init__(self, config: Config, cards: list[Card], projections: dict[str, Projection]):
        self.config = config
        self.projections = projections
        self.slots: dict[str, list[str]] = config.get_path("lineup.slots", {})
        blacklist = set(config.get("blacklist_cards", []))
        # Sestavy, které nešlo obsadit — runner je hlásí uživateli.
        self.skipped: list[str] = []

        self.cards = [
            c
            for c in cards
            if c.slug not in blacklist
            and c.slug in projections
            and projections[c.slug].playable
        ]

    # ------------------------------------------------------------------ public

    def solve(self, tournaments: list[Tournament], time_limit: int = 60) -> list[Lineup]:
        """Postaví co nejvíc sestav.

        Když na všechny požadované sestavy nestačí karty, ubírá je od těch
        nejméně důležitých, dokud řešení neexistuje. Dřív v takové situaci
        spadl celý běh, takže kvůli chybějícímu SP do Hot Streaks nevznikly
        ani Challenger sestavy.
        """
        if not self.cards:
            raise OptimizationError(
                "Žádná použitelná karta. Buď nikdo nehraje, nebo se nepovedlo "
                "napárovat hráče na MLB rostery (zkontroluj varování z `build`)."
            )

        # (turnaj, pořadové číslo sestavy)
        lineup_keys: list[tuple[Tournament, int]] = []
        for tour in tournaments:
            for i in range(max(tour.max_lineups, 0)):
                lineup_keys.append((tour, i))

        if not lineup_keys:
            raise OptimizationError("Žádné turnaje k obsazení.")

        # Ubíráme od nejnižší priority a od nejvyššího pořadí.
        order = sorted(
            range(len(lineup_keys)),
            key=lambda i: (lineup_keys[i][0].weight, -lineup_keys[i][1]),
        )
        self.skipped = []
        attempt = list(lineup_keys)
        droppable = list(order)

        last_error: OptimizationError | None = None
        while attempt:
            try:
                return self._solve_for(attempt, time_limit)
            except OptimizationError as exc:
                last_error = exc
                if len(attempt) == 1:
                    break
                victim_index = droppable.pop(0)
                victim = lineup_keys[victim_index]
                self.skipped.append(f"{victim[0].name} #{victim[1] + 1}")
                attempt = [k for k in attempt if k is not victim]

        raise last_error or OptimizationError("Sestavu nelze postavit.")

    def _solve_for(
        self, lineup_keys: list[tuple[Tournament, int]], time_limit: int
    ) -> list[Lineup]:

        problem = pulp.LpProblem("sorare_mlb_lineups", pulp.LpMaximize)
        x: dict[tuple[str, int, str], pulp.LpVariable] = {}

        for li, (tour, idx) in enumerate(lineup_keys):
            for slot, allowed in self.slots.items():
                for card in self.cards:
                    if not set(card.positions) & set(allowed):
                        continue
                    if not self._eligible(card, tour):
                        continue
                    x[(card.slug, li, slot)] = pulp.LpVariable(
                        f"x_{_safe(card.slug)}_{li}_{slot}", cat="Binary"
                    )

        if not x:
            raise OptimizationError(
                "Model je prázdný — žádná karta nesedí do žádného slotu. "
                "Nejčastější příčina: špatné mapování pozic v config.yaml → `lineup.slots`."
            )

        # 1) každý slot právě jednou
        for li, (tour, idx) in enumerate(lineup_keys):
            for slot in self.slots:
                vars_in_slot = [v for (c, l, s), v in x.items() if l == li and s == slot]
                if not vars_in_slot:
                    raise OptimizationError(
                        f"{tour.name} #{idx + 1}: slot {slot} nelze obsadit — "
                        "žádná karta na tu pozici."
                    )
                problem += pulp.lpSum(vars_in_slot) == 1, f"slot_{li}_{slot}"

        # 1b) Startující nadhazovači: dokud je k dispozici někdo s ohlášeným
        # startem, nikdo jiný do SP slotu nesmí. Penalizace v projekci na tohle
        # nestačila — silná forma ji dokázala přebít.
        # Slot jen pro startující nadhazovače: všechny povolené pozice
        # obsahují STARTING_PITCHER. Navázáno na název pozice, ne na
        # pomocný seznam v configu, který nemusí být vyplněný.
        sp_slots = [
            slot for slot, allowed in self.slots.items()
            if allowed and all("STARTING_PITCHER" in p.upper() for p in allowed)
        ]
        starters = {c.slug for c in self.cards if c.sorare_probable_starter is True}
        if starters:
            for slot in sp_slots:
                if not any(
                    c in starters
                    for (c, l, s) in x
                    if s == slot
                ):
                    continue  # na tenhle slot nikdo se startem není
                for (c, l, s), var in x.items():
                    if s == slot and c not in starters:
                        problem += var == 0, f"needstart_{l}_{slot}_{_safe(c)}"

        # 2) karta nejvýš jednou celkově
        for card in self.cards:
            uses = [v for (c, l, s), v in x.items() if c == card.slug]
            if uses:
                problem += pulp.lpSum(uses) <= 1, f"unique_{_safe(card.slug)}"

        # 2b) jeden hráč jen jednou v rámci jedné sestavy.
        # Sorare tohle odmítá ("One player can only appear once") a portfolio
        # běžně obsahuje víc karet téhož hráče.
        by_player: dict[str, list[str]] = defaultdict(list)
        for card in self.cards:
            if card.player.slug:
                by_player[card.player.slug].append(card.slug)

        for li, _ in enumerate(lineup_keys):
            for player_slug, slugs in by_player.items():
                if len(slugs) < 2:
                    continue
                uses = [v for (c, l, s), v in x.items() if l == li and c in set(slugs)]
                if len(uses) > 1:
                    problem += (
                        pulp.lpSum(uses) <= 1,
                        f"oneplayer_{li}_{_safe(player_slug)}",
                    )

        # 5) limit hráčů z jednoho týmu
        max_from_team = int(self.config.get_path("stack.max_from_team", 4))
        by_team: dict[str, list[Card]] = defaultdict(list)
        for card in self.cards:
            if card.player.team_slug:
                by_team[card.player.team_slug].append(card)

        for li, _ in enumerate(lineup_keys):
            for team, team_cards in by_team.items():
                uses = [
                    v for (c, l, s), v in x.items()
                    if l == li and c in {tc.slug for tc in team_cards}
                ]
                if len(uses) > max_from_team:
                    problem += pulp.lpSum(uses) <= max_from_team, f"team_{li}_{_safe(team)}"

        # 5b) in-season minimum.
        # Sorare to hlásí jako "minimum required is 6" — vyjadřuje se to jako
        # dolní mez na karty se season bonusem, ne jako strop na ty staré.
        for li, (tour, _) in enumerate(lineup_keys):
            if tour.min_in_season is None:
                continue
            fresh = {c.slug for c in self.cards if c.in_season}
            uses = [v for (c, l, s), v in x.items() if l == li and c in fresh]
            problem += pulp.lpSum(uses) >= tour.min_in_season, f"inseason_{li}"

        # 6) floor pro bezpečné sestavy
        min_floor = float(self.config.get_path("safety.min_projected_floor", 0))
        if min_floor > 0:
            for li, (tour, _) in enumerate(lineup_keys):
                if tour.risk_mode != "safe":
                    continue
                for (c, l, s), var in x.items():
                    if l != li:
                        continue
                    if self.projections[c].floor < min_floor:
                        problem += var == 0, f"floor_{li}_{_safe(c)}_{s}"

        # účelová funkce
        objective = []
        for (c, li, slot), var in x.items():
            tour = lineup_keys[li][0]
            proj = self.projections[c]
            value = proj.floor if tour.risk_mode == "safe" else proj.ceiling
            # Průměr držíme v mixu, aby safe režim nevybíral jen nudné jistoty.
            value = 0.65 * value + 0.35 * proj.mean
            objective.append(tour.weight * value * var)

        stack_terms, stack_constraints = self._stack_terms(x, lineup_keys, by_team)
        objective.extend(stack_terms)
        for name, constraint in stack_constraints:
            problem += constraint, name

        problem += pulp.lpSum(objective)

        status = problem.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=time_limit))
        if pulp.LpStatus[status] != "Optimal":
            # Bez rozpadu po slotech se infeasibilita ladí naslepo.
            supply = []
            needed = len(lineup_keys)
            for slot, allowed in self.slots.items():
                fits = [c for c in self.cards if set(c.positions) & set(allowed)]
                # Práh na floor platí jen pro "safe" turnaje, ne pro všechny.
                above = [c for c in fits if self.projections[c.slug].floor >= min_floor]
                mark = "!" if len(fits) < needed else " "
                detail = f"  {mark} {slot}: {len(fits)} karet / {needed} potřeba"
                if min_floor > 0 and len(above) < needed:
                    detail += f" (z toho {len(above)} nad floor {min_floor})"
                supply.append(detail)

            raise OptimizationError(
                f"Solver skončil se stavem {pulp.LpStatus[status]}. Nejčastější příčiny:\n"
                "  • málo hrajících karet na požadovaný počet sestav → sniž `max_lineups`,\n"
                f"  • `safety.min_projected_floor` ({min_floor}) vyřadil příliš mnoho karet,\n"
                f"  • `stack.max_from_team` ({max_from_team}) blokuje sestavu, protože máš "
                "karty jen z pár týmů.\n\n"
                "Nabídka po slotech (! = nedostatek):\n" + "\n".join(supply)
            )

        return self._extract(x, lineup_keys)

    # ------------------------------------------------------------------ stacking

    def _stack_terms(self, x, lineup_keys, by_team):
        """Odměna za koncentraci pálkařů z jednoho týmu.

        Lineárně: zavedeme pomocné binárky s[li, team, k] = "v sestavě li je
        alespoň k+1 pálkařů z týmu", každá s bonusem. Suma je konkávní, takže
        model nemá důvod bonus nafouknout nad reálný počet.
        """
        if not self.config.get_path("stack.enabled", True):
            return [], []

        bonus = float(self.config.get_path("stack.bonus", 0))
        apply_to = set(self.config.get_path("stack.apply_to", []))
        max_team = int(self.config.get_path("stack.max_from_team", 4))
        hitters = set(self.config.get_path("lineup.hitter_positions", []))

        terms, constraints = [], []
        for li, (tour, _) in enumerate(lineup_keys):
            if apply_to and tour.name not in apply_to:
                continue
            for team, team_cards in by_team.items():
                hitter_slugs = {
                    c.slug for c in team_cards if set(c.positions) & hitters
                }
                uses = [v for (c, l, s), v in x.items() if l == li and c in hitter_slugs]
                if len(uses) < 2:
                    continue
                count_expr = pulp.lpSum(uses)
                for k in range(1, min(max_team, len(uses))):
                    aux = pulp.LpVariable(f"stack_{li}_{_safe(team)}_{k}", cat="Binary")
                    constraints.append(
                        (f"stackc_{li}_{_safe(team)}_{k}", aux * (k + 1) <= count_expr)
                    )
                    terms.append(tour.weight * bonus * aux)
        return terms, constraints

    # ------------------------------------------------------------------ helpers

    def _eligible(self, card: Card, tour: Tournament) -> bool:
        if tour.allowed_rarities and card.rarity not in tour.allowed_rarities:
            return False
        return True

    def _extract(self, x, lineup_keys) -> list[Lineup]:
        by_card = {c.slug: c for c in self.cards}
        lineups: list[Lineup] = []
        for li, (tour, idx) in enumerate(lineup_keys):
            slots: list[LineupSlot] = []
            for (c, l, slot), var in x.items():
                if l != li or var.value() is None or var.value() < 0.5:
                    continue
                card = by_card[c]
                slots.append(
                    LineupSlot(
                        slot=slot,
                        card_slug=c,
                        player_name=card.player.name,
                        team=card.player.team_name,
                        projected=self.projections[c].mean,
                    )
                )
            if not slots:
                continue
            order = list(self.slots.keys())
            slots.sort(key=lambda s: order.index(s.slot))
            lineups.append(
                Lineup(
                    tournament_slug=tour.slug,
                    tournament_name=tour.name,
                    index=idx,
                    slots=slots,
                )
            )
        return lineups


def _safe(text: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in text)
