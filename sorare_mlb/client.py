"""GraphQL klient pro Sorare — serverless varianta.

Cache leží ve Store (Redis), ne v SQLite. To je nutné nejen kvůli read-only
filesystemu, ale hlavně proto, že pipeline běží přes několik invokací funkce
a každá musí navázat na to, co stáhla ta předchozí.
"""
from __future__ import annotations

import time

import requests

from . import queries
from .auth import Token, ensure_token
from .models import Card, Config, Player, env
from .store import K_CARDS, K_RECENT_LINEUPS, K_UPCOMING, get_store


class SorareError(RuntimeError):
    pass


class SorareClient:
    def __init__(self, config: Config, token: Token | None = None):
        self.config = config
        self.url = config.get_path("api.sorare_url", "https://api.sorare.com/graphql")
        self.token = token or ensure_token()
        self.api_key = env("SORARE_API_KEY")
        self.store = get_store()
        self.cache_ttl = int(config.get_path("api.cache_ttl_minutes", 30)) * 60
        self.session = requests.Session()
        self._min_interval = 60.0 / max(int(config.get_path("api.requests_per_minute", 40)), 1)
        self._last_call = 0.0

    # ------------------------------------------------------------------ core

    def _headers(self) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "Authorization": f"Bearer {self.token.access_token}",
            "JWT-AUD": self.token.aud,
        }
        if self.api_key:
            headers["APIKEY"] = self.api_key
        return headers

    def _throttle(self) -> None:
        delta = time.monotonic() - self._last_call
        if delta < self._min_interval:
            time.sleep(self._min_interval - delta)
        self._last_call = time.monotonic()

    def execute(
        self,
        query: str,
        variables: dict | None = None,
        operation_name: str | None = None,
        retries: int = 3,
        tolerate_errors: bool = False,
    ) -> dict:
        payload = {"query": query, "variables": variables or {}}
        if operation_name:
            payload["operationName"] = operation_name

        last: Exception | None = None
        for attempt in range(retries):
            self._throttle()
            try:
                resp = self.session.post(
                    self.url, headers=self._headers(), json=payload, timeout=30
                )
            except requests.RequestException as exc:
                last = exc
                time.sleep(1.5 ** attempt)
                continue

            if resp.status_code == 429:
                time.sleep(min(int(resp.headers.get("Retry-After", 3)), 10))
                continue
            if resp.status_code == 401 and attempt == 0:
                self.token = ensure_token(force=True)
                continue
            if resp.status_code >= 500:
                time.sleep(1.5 ** attempt)
                continue

            if resp.status_code in (400, 422):
                # Neplatný dotaz. Detail je v těle odpovědi — bez něj se
                # nedá poznat, které pole Sorare přejmenovalo.
                try:
                    detail = resp.json()
                    messages = "; ".join(
                        e.get("message", "?") for e in (detail.get("errors") or [])
                    ) or str(detail)[:400]
                except ValueError:
                    messages = resp.text[:400]
                raise SorareError(
                    f"Sorare odmítlo dotaz ({operation_name or 'bez názvu'}): {messages}\n"
                    "Schéma se nejspíš změnilo — otevři /api/probe a uprav queries.py."
                )

            resp.raise_for_status()
            body = resp.json()
            if body.get("errors") and not tolerate_errors:
                raise SorareError(
                    "GraphQL chyba: "
                    + "; ".join(e.get("message", "?") for e in body["errors"])
                )
            return body

        raise SorareError(f"Request selhal po {retries} pokusech: {last}")

    # ------------------------------------------------------------------ data

    def _with_vault(self, query: str) -> str:
        """Doplní do dotazu na karty příznak trezoru, pokud ho schéma má."""
        try:
            from .features import get_features

            selection = (get_features().get("vault") or {}).get("selection")
        except Exception:  # noqa: BLE001 — bez schématu jedeme bez trezoru
            selection = None
        if not selection:
            return query
        return query.replace("anyPositions", f"anyPositions\n        {selection}", 1)

    def fetch_cards(self, use_cache: bool = True) -> list[Card]:
        """Karty i s posledními skóre — obojí přijde v jedné odpovědi."""
        if use_cache:
            cached = self.store.get_json(K_CARDS)
            if cached:
                return [card_from_dict(c) for c in cached]

        nodes: list[dict] = []
        cursor: str | None = None
        while True:
            body = self.execute(
                self._with_vault(queries.USER_CARDS),
                {"rarities": self.config.get("rarities", ["limited", "rare"]), "after": cursor},
                operation_name="UserBaseballCards",
            )
            block = ((body.get("data") or {}).get("currentUser") or {}).get("cards") or {}
            nodes.extend(block.get("nodes") or [])
            page = block.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                break
            cursor = page.get("endCursor")

        # Portfolio se mění zřídka, ale skóre ano — 2 h je rozumný kompromis.
        self.store.set(K_CARDS, nodes, ttl_seconds=2 * 3600)
        return [card_from_dict(n) for n in nodes]

    def fetch_scores(self, cards: list[Card]) -> dict[str, dict]:
        """Skóre už máme z fetch_cards; tohle je jen přerovnání podle hráče."""
        out: dict[str, dict] = {}
        for card in cards:
            if card.player.slug and card.player.slug not in out:
                out[card.player.slug] = {
                    "scores": card.recent_scores,
                    "last_game": card.last_game,
                }
        return out

    def fetch_probable_starters(
        self, fixture_slug: str, min_odds: int = 5000
    ) -> tuple[set[str], str | None]:
        """Slugy nadhazovačů ohlášených jako startéři v tomhle gameweeku.

        Zdrojem jsou zápasy fixture — to je to, z čeho Sorare kreslí odznak
        "PP" na kartě. `min_odds` se nepoužívá, zůstává kvůli kompatibilitě
        volání.

        Vrací (slugy, chyba). Chybu vracíme ven, ať je vidět v logu: jinak
        se tiše postaví sestava s nadhazovači, kteří nenastoupí.
        """
        body = self.execute(
            queries.PROBABLE_STARTERS,
            {"slug": fixture_slug},
            operation_name="FixtureProbablePitchers",
            tolerate_errors=True,
        )
        if body.get("errors"):
            return set(), "; ".join(
                e.get("message", "?") for e in body["errors"]
            )[:300]

        fixture = ((body.get("data") or {}).get("so5") or {}).get("so5Fixture") or {}
        games = fixture.get("anyGames") or []
        if not games:
            return set(), "fixture nevrátil žádné zápasy"

        found = {
            p.get("slug")
            for game in games
            for p in (game.get("probablePitchers") or [])
            if p.get("slug")
        }
        return found, None if found else f"v {len(games)} zápasech nikdo ohlášený"

    def fetch_bench(
        self, leaderboard_slug: str, filters: dict, limit_pages: int = 6
    ) -> tuple[list[dict], str | None]:
        """Lavička leaderboardu s libovolnými filtry. Pro ladění i pro provoz."""
        nodes: list[dict] = []
        cursor: str | None = None

        for _ in range(limit_pages):
            body = self.execute(
                queries.BENCH_PROBE,
                {"slug": leaderboard_slug, "filters": filters, "after": cursor},
                operation_name="BenchProbe",
                tolerate_errors=True,
            )
            if body.get("errors"):
                return [], "; ".join(
                    e.get("message", "?") for e in body["errors"]
                )[:300]

            board = ((body.get("data") or {}).get("so5") or {}).get("so5Leaderboard") or {}
            bench = board.get("myFilteredBench")
            if bench is None:
                return [], "myFilteredBench nevrátil nic"

            nodes.extend(bench.get("nodes") or [])
            page = bench.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                break
            cursor = page.get("endCursor")

        return nodes, None

    def fetch_leaderboards(self, sport: str = "BASEBALL") -> list[dict]:
        """Otevřené leaderboardy daného sportu — to, čemu v configu říkáme turnaje."""
        return [
            b for b in self.fetch_all_upcoming()
            if str(((b.get("so5Fixture") or {}).get("sport") or "")).upper() == sport
        ]

    def fetch_all_upcoming(self, cache_seconds: int = 0) -> list[dict]:
        """Otevřené leaderboardy napříč sporty (jeden dotaz pro všechny)."""
        if cache_seconds:
            cached = self.store.get_json(K_UPCOMING)
            if cached is not None:
                return cached
        body = self.execute(queries.UPCOMING_LEADERBOARDS, operation_name="UpcomingLeaderboards")
        boards = ((body.get("data") or {}).get("so5") or {}).get("upcomingLeaderboards") or []
        if cache_seconds:
            self.store.set(K_UPCOMING, boards, ttl_seconds=cache_seconds)
        return boards

    # ------------------------------------------------------------------ přehledy

    def execute_first_ok(self, tiers: list[tuple[str, str]], variables: dict | None = None) -> tuple[dict, str]:
        """Zkusí dotazy od nejbohatšího; vrátí první, který Sorare přijme.

        Vrací (data, název použité varianty). Když neprojde žádný, vyhodí
        poslední chybu — ať je v UI vidět proč.
        """
        last: Exception | None = None
        for name, query in tiers:
            try:
                body = self.execute(query, variables, operation_name=name, retries=2)
                return body.get("data") or {}, name
            except SorareError as exc:
                last = exc
        raise SorareError(f"Žádná varianta dotazu neprošla: {last}")

    def fetch_sport_cards(
        self, sport: str, rarities: list[str], max_pages: int = 15
    ) -> tuple[list[dict], bool]:
        """Syrové uzly karet pro přehled. Vrací (uzly, zda byl výpis useknut)."""
        nodes: list[dict] = []
        cursor: str | None = None
        for _ in range(max_pages):
            body = self.execute(
                self._with_vault(queries.SPORT_CARDS),
                {"sport": sport, "rarities": rarities, "after": cursor},
                operation_name="UserSportCards",
            )
            block = ((body.get("data") or {}).get("currentUser") or {}).get("cards") or {}
            nodes.extend(block.get("nodes") or [])
            page = block.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                return nodes, False
            cursor = page.get("endCursor")
        return nodes, True

    def fetch_recent_lineups(self, cache_seconds: int = 600) -> dict:
        """Probíhající a nedávné sestavy napříč sporty."""
        cached = self.store.get_json(K_RECENT_LINEUPS)
        if cached is not None:
            return cached
        data, variant = self.execute_first_ok(queries.RECENT_LINEUPS_TIERS)
        lineups = ((data.get("so5") or {}).get("myOngoingAndRecentSo5Lineups")) or []
        result = {"variant": variant, "lineups": lineups}
        self.store.set(K_RECENT_LINEUPS, result, ttl_seconds=cache_seconds)
        return result

    # ------------------------------------------------------------------ mutation

    def submit_lineup(
        self,
        leaderboard_id: str,
        card_slugs: list[str],
        manager_team_id: str | None = None,
        requires_manager_team: bool = False,
    ) -> dict:
        """Odešle sestavu. Chce ID leaderboardu, ne slug.

        `captain` je v So5AppearanceInput povinný; v MLB kapitána neřešíme,
        takže posíláme false u všech. Některé soutěže (Challenger) vyžadují
        manager team — když ho uživatel nemá, necháme ho Sorare založit.
        """
        appearances = [
            {"cardSlug": slug, "captain": False, "index": i}
            for i, slug in enumerate(card_slugs)
        ]
        payload: dict = {
            "so5LeaderboardId": leaderboard_id,
            "so5Appearances": appearances,
        }
        # Hot Streak Champion manager team nepovoluje vůbec ("can't have more
        # than 0"), Challenger ho naopak vyžaduje pro každou sestavu zvlášť.
        if manager_team_id:
            payload["managerTeamId"] = manager_team_id
        elif requires_manager_team:
            payload["shouldCreateManagerTeam"] = True

        body = self.execute(
            queries.SUBMIT_LINEUP,
            {"input": payload},
            operation_name="CreateOrUpdateSo5Lineup",
            tolerate_errors=True,
        )
        if body.get("errors"):
            raise SorareError(f"Sorare odmítlo mutaci: {body['errors']}")

        result = (body.get("data") or {}).get("createOrUpdateSo5Lineup") or {}
        if result.get("errors"):
            messages = "; ".join(e.get("message", "?") for e in result["errors"])
            raise SorareError(f"Sorare sestavu odmítl: {messages}")
        return result

    # ------------------------------------------------------------------ probe

    def introspect_root(self) -> dict:
        body = self.execute(queries.INTROSPECT_ROOT, operation_name="IntrospectRoot")
        return (body.get("data") or {}).get("__schema") or {}

    def introspect_type(self, name: str) -> dict | None:
        body = self.execute(
            queries.INTROSPECT_TYPE, {"name": name},
            operation_name="IntrospectType", tolerate_errors=True,
        )
        return (body.get("data") or {}).get("__type")


def _current_season() -> int:
    """Sezóna, která právě dává season bonus.

    MLB sezóna se kryje s kalendářním rokem, takže stačí rok — a v lednu
    a únoru, kdy se ještě nehraje, platí ta předchozí.
    """
    from datetime import date

    today = date.today()
    return today.year if today.month >= 3 else today.year - 1


def card_from_dict(node: dict) -> Card:
    raw = node.get("anyPlayer") or {}
    team = node.get("anyTeam") or {}

    # Pozice chodí VELKÝMI písmeny (STARTING_PITCHER); držíme je tak,
    # jak přijdou, a config.yaml je mapuje stejně.
    positions = [str(p).upper() for p in (node.get("anyPositions") or [])]

    scores, dates = [], []
    for entry in raw.get("playerGameScores") or []:
        if entry is None or entry.get("score") is None:
            continue
        scores.append(float(entry["score"]))
        date = (entry.get("anyGame") or {}).get("date")
        if date:
            dates.append(date)

    player = Player(
        slug=raw.get("slug", ""),
        name=raw.get("displayName", "?"),
        positions=positions,
        team_slug=team.get("slug"),
        team_name=team.get("name"),
    )
    return Card(
        slug=node.get("slug", ""),
        rarity=str(node.get("rarityTyped", "")).lower(),
        season=node.get("seasonYear"),
        player=player,
        recent_scores=scores,
        last_game=max(dates) if dates else None,
        # `inSeasonEligible` neodpovídá season bonusu, jak ho počítá Sorare
        # při validaci sestavy — rozhoduje ročník karty.
        in_season=int(node.get("seasonYear") or 0) >= _current_season(),
        sorare_projection=_as_float(raw.get("nextClassicFixtureProjectedScore")),
        in_vault=bool(node.get("vaultFlag")),
    )


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _hash(slugs: list[str]) -> str:
    import hashlib

    return hashlib.sha1(",".join(sorted(slugs)).encode()).hexdigest()[:12]
