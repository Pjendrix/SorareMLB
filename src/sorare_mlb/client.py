"""HTTP/GraphQL klient pro Sorare.

Řeší tři věci, které skripty na Sorare API typicky odflákávají:
* rate limiting (Sorare vrací 429 a bez API klíče narazíš rychle),
* obnovu vypršelého JWT,
* cachování portfolia, aby se 300 karet netahalo při každém běhu.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import requests

from . import queries
from .auth import Token, ensure_token
from .models import Card, Config, Player, env

CACHE_DB = Path(".cache.sqlite")


class SorareError(RuntimeError):
    pass


class _RateLimiter:
    def __init__(self, per_minute: int):
        self.interval = 60.0 / max(per_minute, 1)
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            delta = time.monotonic() - self._last
            if delta < self.interval:
                time.sleep(self.interval - delta)
            self._last = time.monotonic()


class _Cache:
    def __init__(self, ttl_minutes: int):
        self.ttl = ttl_minutes * 60
        self.conn = sqlite3.connect(CACHE_DB)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT, ts REAL)"
        )
        self.conn.commit()

    def get(self, key: str) -> Any | None:
        row = self.conn.execute("SELECT value, ts FROM cache WHERE key=?", (key,)).fetchone()
        if not row or time.time() - row[1] > self.ttl:
            return None
        return json.loads(row[0])

    def set(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO cache VALUES (?,?,?)", (key, json.dumps(value), time.time())
        )
        self.conn.commit()

    def clear(self) -> None:
        self.conn.execute("DELETE FROM cache")
        self.conn.commit()


class SorareClient:
    def __init__(self, config: Config, token: Token | None = None, auth_method: str = "password"):
        self.config = config
        self.url = config.get_path("api.sorare_url", "https://api.sorare.com/graphql")
        self.token = token or ensure_token(auth_method)
        self.api_key = env("SORARE_API_KEY")
        self.auth_method = auth_method
        self.limiter = _RateLimiter(config.get_path("api.requests_per_minute", 40))
        self.cache = _Cache(config.get_path("api.cache_ttl_minutes", 30))
        self.session = requests.Session()

    # ------------------------------------------------------------------ core

    def _headers(self) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "Authorization": f"Bearer {self.token.access_token}",
            # Bez JWT-AUD server token odmítne. Musí sedět s aud ze signIn.
            "JWT-AUD": self.token.aud,
        }
        if self.api_key:
            headers["APIKEY"] = self.api_key
        return headers

    def execute(
        self,
        query: str,
        variables: dict | None = None,
        operation_name: str | None = None,
        retries: int = 4,
        tolerate_errors: bool = False,
    ) -> dict:
        payload = {"query": query, "variables": variables or {}}
        if operation_name:
            payload["operationName"] = operation_name

        last_error: Exception | None = None
        for attempt in range(retries):
            self.limiter.wait()
            try:
                resp = self.session.post(
                    self.url, headers=self._headers(), json=payload, timeout=45
                )
            except requests.RequestException as exc:
                last_error = exc
                time.sleep(2 ** attempt)
                continue

            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 2 ** (attempt + 2)))
                time.sleep(wait)
                continue

            if resp.status_code == 401:
                # Token vypršel nebo byl odvolán — jednou zkusíme obnovit.
                if attempt == 0:
                    self.token = ensure_token(self.auth_method, force=True)
                    continue
                raise SorareError("Autentizace selhala i po obnově tokenu.")

            if resp.status_code >= 500:
                time.sleep(2 ** attempt)
                continue

            resp.raise_for_status()
            body = resp.json()

            if body.get("errors") and not tolerate_errors:
                raise SorareError(
                    "GraphQL chyba: "
                    + "; ".join(e.get("message", "?") for e in body["errors"])
                    + "\nTip: spusť `python -m sorare_mlb probe` — schéma se mohlo změnit."
                )
            return body

        raise SorareError(f"Request selhal po {retries} pokusech: {last_error}")

    # ------------------------------------------------------------------ data

    def fetch_cards(self, use_cache: bool = True) -> list[Card]:
        rarities = self.config.get("rarities", ["limited", "rare"])
        cache_key = f"cards:{','.join(rarities)}"
        if use_cache:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return [_card_from_dict(c) for c in cached]

        nodes: list[dict] = []
        cursor: str | None = None
        while True:
            body = self.execute(
                queries.USER_CARDS,
                {"rarities": rarities, "after": cursor},
                operation_name="UserBaseballCards",
            )
            block = ((body.get("data") or {}).get("currentUser") or {}).get("baseballCards") or {}
            nodes.extend(block.get("nodes") or [])
            page = block.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                break
            cursor = page.get("endCursor")

        self.cache.set(cache_key, nodes)
        return [_card_from_dict(n) for n in nodes]

    def fetch_scores(self, player_slugs: list[str]) -> dict[str, list[float]]:
        """Posledních 15 Sorare skóre pro každého hráče. Batchuje po 30."""
        out: dict[str, list[float]] = {}
        for i in range(0, len(player_slugs), 30):
            batch = player_slugs[i : i + 30]
            cache_key = "scores:" + ",".join(sorted(batch))
            cached = self.cache.get(cache_key)
            if cached is not None:
                out.update(cached)
                continue

            body = self.execute(
                queries.PLAYER_SCORES, {"slugs": batch}, operation_name="PlayerScores"
            )
            chunk: dict[str, list[float]] = {}
            for node in (body.get("data") or {}).get("baseballPlayers") or []:
                stats = (node.get("gameStats") or {}).get("nodes") or []
                chunk[node["slug"]] = [
                    float(s["score"]) for s in stats if s.get("score") is not None
                ]
            self.cache.set(cache_key, chunk)
            out.update(chunk)
        return out

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
        """Odešle sestavu. Zkusí primární tvar mutace, pak fallback."""
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
        raise SorareError(
            "Ani jedna varianta submit mutace neprošla.\n"
            + "\n".join(errors)
            + "\nSpusť `probe` a uprav queries.py podle aktuálního schématu."
        )

    # ------------------------------------------------------------------ probe

    def introspect_root(self) -> dict:
        body = self.execute(queries.INTROSPECT_ROOT, operation_name="IntrospectRoot")
        return (body.get("data") or {}).get("__schema") or {}

    def introspect_type(self, name: str) -> dict | None:
        body = self.execute(
            queries.INTROSPECT_TYPE, {"name": name}, operation_name="IntrospectType",
            tolerate_errors=True,
        )
        return (body.get("data") or {}).get("__type")


def _card_from_dict(node: dict) -> Card:
    raw_player = node.get("player") or {}
    team = raw_player.get("team") or {}
    player = Player(
        slug=raw_player.get("slug", ""),
        name=raw_player.get("displayName", "?"),
        positions=[str(p).lower() for p in (raw_player.get("positions") or [])],
        team_slug=team.get("slug"),
        team_name=team.get("abbreviation") or team.get("name"),
    )
    return Card(
        slug=node.get("slug", ""),
        rarity=str(node.get("rarity", "")).lower(),
        season=node.get("seasonYear"),
        player=player,
    )
