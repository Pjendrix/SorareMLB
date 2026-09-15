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
from .store import K_CARDS, K_SCORES, get_store


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

    def fetch_cards(self, use_cache: bool = True) -> list[Card]:
        if use_cache:
            cached = self.store.get_json(K_CARDS)
            if cached:
                return [card_from_dict(c) for c in cached]

        nodes: list[dict] = []
        cursor: str | None = None
        while True:
            body = self.execute(
                queries.USER_CARDS,
                {"rarities": self.config.get("rarities", ["limited", "rare"]), "after": cursor},
                operation_name="UserBaseballCards",
            )
            block = ((body.get("data") or {}).get("currentUser") or {}).get("baseballCards") or {}
            nodes.extend(block.get("nodes") or [])
            page = block.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                break
            cursor = page.get("endCursor")

        # Portfolio se mění zřídka — držíme ho 12 h, ať se pipeline nezdržuje.
        self.store.set(K_CARDS, nodes, ttl_seconds=12 * 3600)
        return [card_from_dict(n) for n in nodes]

    def fetch_scores_batch(self, slugs: list[str]) -> dict[str, dict]:
        """Jedna dávka hráčů: skóre + datum posledního zápasu.

        Datum bereme ze stejné odpovědi, takže filtr neaktivních hráčů
        nestojí ani jedno volání navíc.
        """
        key = K_SCORES.format(batch=_hash(slugs))
        cached = self.store.get_json(key)
        if cached is not None:
            return cached

        body = self.execute(
            queries.PLAYER_SCORES, {"slugs": slugs}, operation_name="PlayerScores"
        )
        chunk: dict[str, dict] = {}
        for node in (body.get("data") or {}).get("baseballPlayers") or []:
            stats = (node.get("gameStats") or {}).get("nodes") or []
            played = [s for s in stats if s.get("score") is not None]
            dates = [
                (s.get("game") or {}).get("startDate")
                for s in played
                if (s.get("game") or {}).get("startDate")
            ]
            chunk[node["slug"]] = {
                "scores": [float(s["score"]) for s in played],
                "last_game": max(dates) if dates else None,
            }

        self.store.set(key, chunk, ttl_seconds=6 * 3600)
        return chunk

    def fetch_open_fixture(self) -> dict | None:
        body = self.execute(queries.UPCOMING_FIXTURES, operation_name="UpcomingBaseballFixtures")
        nodes = (((body.get("data") or {}).get("baseball") or {}).get("allFixtures") or {}).get(
            "nodes"
        ) or []
        for node in nodes:
            if str(node.get("state", "")).lower() in ("opened", "open", "upcoming"):
                return node
        return nodes[0] if nodes else None

    def fetch_competitions(self, fixture_slug: str) -> list[dict]:
        body = self.execute(
            queries.FIXTURE_TOURNAMENTS,
            {"fixtureSlug": fixture_slug},
            operation_name="FixtureCompetitions",
        )
        fixture = ((body.get("data") or {}).get("baseball") or {}).get("fixture") or {}
        return ((fixture.get("competitions") or {}).get("nodes")) or []

    # ------------------------------------------------------------------ mutation

    def submit_lineup(self, competition_slug: str, card_slugs: list[str]) -> dict:
        variants = [
            (queries.SUBMIT_LINEUP_PRIMARY, "CreateBaseballLineup", "createBaseballLineup"),
            (queries.SUBMIT_LINEUP_FALLBACK, "SubmitLineup", "submitLineup"),
        ]
        errors: list[str] = []
        for mutation, op_name, field in variants:
            body = self.execute(
                mutation,
                {"input": {"competitionSlug": competition_slug, "cardSlugs": card_slugs}},
                operation_name=op_name,
                tolerate_errors=True,
            )
            if body.get("errors"):
                errors.append(str(body["errors"]))
                continue
            result = (body.get("data") or {}).get(field) or {}
            if result.get("errors"):
                raise SorareError(f"Sorare sestavu odmítl: {result['errors']}")
            return result
        raise SorareError("Submit mutace neprošla ani v jedné variantě: " + " | ".join(errors))

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


def card_from_dict(node: dict) -> Card:
    raw = node.get("player") or {}
    team = raw.get("team") or {}
    player = Player(
        slug=raw.get("slug", ""),
        name=raw.get("displayName", "?"),
        positions=[str(p).lower() for p in (raw.get("positions") or [])],
        team_slug=team.get("slug"),
        team_name=team.get("abbreviation") or team.get("name"),
    )
    return Card(
        slug=node.get("slug", ""),
        rarity=str(node.get("rarity", "")).lower(),
        season=node.get("seasonYear"),
        player=player,
    )


def _hash(slugs: list[str]) -> str:
    import hashlib

    return hashlib.sha1(",".join(sorted(slugs)).encode()).hexdigest()[:12]
