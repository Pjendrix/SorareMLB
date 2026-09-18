"""Úložiště stavu.

Na Vercelu je filesystem read-only a mezi běhy prázdný, takže tokeny, cache
i rozpracované joby musí žít mimo funkci. Používáme Upstash Redis přes REST
(free tier, funguje ze serverless bez TCP spojení).

Lokálně, když proměnné nejsou nastavené, spadneme na JSON soubor — díky tomu
jde všechno vyzkoušet na notebooku bez závislosti na cloudu.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import requests

LOCAL_FILE = Path(".local-store.json")


class StoreUnavailable(RuntimeError):
    """Úložiště není nakonfigurované nebo do něj nelze zapsat."""


class Store:
    """Jednoduché key-value s TTL."""

    def get(self, key: str) -> Any | None:
        raise NotImplementedError

    def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError

    def push_capped(self, key: str, value: Any, limit: int) -> None:
        """Přidá položku na začátek seznamu a ořízne ho na `limit`."""
        raise NotImplementedError

    def read_list(self, key: str, limit: int = 100) -> list:
        """Nejnovější položky seznamu (už rozparsované z JSON)."""
        raise NotImplementedError

    def acquire(self, key: str, ttl_seconds: int) -> bool:
        """Zámek: nastaví klíč jen když neexistuje. True = zámek je náš."""
        raise NotImplementedError

    # --- pohodlné obaly ---

    def get_json(self, key: str, default: Any = None) -> Any:
        raw = self.get(key)
        if raw is None:
            return default
        if isinstance(raw, (dict, list)):
            return raw
        try:
            return json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return default


class UpstashStore(Store):
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}

    def _cmd(self, *args) -> Any:
        resp = requests.post(
            self.url,
            headers={**self.headers, "Content-Type": "application/json"},
            json=list(args),
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("result")

    def get(self, key: str) -> Any | None:
        return self._cmd("GET", key)

    def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        payload = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        if ttl_seconds:
            self._cmd("SET", key, payload, "EX", str(int(ttl_seconds)))
        else:
            self._cmd("SET", key, payload)

    def delete(self, key: str) -> None:
        self._cmd("DEL", key)

    def acquire(self, key: str, ttl_seconds: int) -> bool:
        return self._cmd("SET", key, "1", "NX", "EX", str(int(ttl_seconds))) == "OK"

    def push_capped(self, key: str, value: Any, limit: int) -> None:
        payload = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        self._cmd("LPUSH", key, payload)
        self._cmd("LTRIM", key, "0", str(limit - 1))

    def read_list(self, key: str, limit: int = 100) -> list:
        raw = self._cmd("LRANGE", key, "0", str(limit - 1)) or []
        return [_loads(r) for r in raw]


class LocalStore(Store):
    """Fallback pro lokální vývoj."""

    _lock = threading.Lock()

    def _read(self) -> dict:
        if not LOCAL_FILE.exists():
            return {}
        try:
            return json.loads(LOCAL_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def _write(self, data: dict) -> None:
        try:
            LOCAL_FILE.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            # Na Vercelu je filesystem read-only. Bez Redisu se stav nemá kam
            # uložit a přihlášení ani pipeline nemůžou fungovat.
            raise StoreUnavailable(
                "Není nakonfigurované úložiště. Přidej v Vercelu integraci "
                "Upstash Redis (Storage → Marketplace) — doplní proměnné "
                "KV_REST_API_URL a KV_REST_API_TOKEN — a udělej redeploy."
            ) from exc

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._read().get(key)
        if not entry:
            return None
        if entry.get("expires_at") and time.time() > entry["expires_at"]:
            return None
        return entry.get("value")

    def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        payload = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        with self._lock:
            data = self._read()
            data[key] = {
                "value": payload,
                "expires_at": time.time() + ttl_seconds if ttl_seconds else None,
            }
            self._write(data)

    def delete(self, key: str) -> None:
        with self._lock:
            data = self._read()
            data.pop(key, None)
            self._write(data)

    def acquire(self, key: str, ttl_seconds: int) -> bool:
        with self._lock:
            data = self._read()
            entry = data.get(key)
            if entry and not (entry.get("expires_at") and time.time() > entry["expires_at"]):
                return False
            data[key] = {"value": "1", "expires_at": time.time() + ttl_seconds}
            self._write(data)
            return True

    def push_capped(self, key: str, value: Any, limit: int) -> None:
        with self._lock:
            data = self._read()
            items = (data.get(key) or {}).get("value") or []
            if not isinstance(items, list):
                items = []
            items.insert(0, value)
            data[key] = {"value": items[:limit], "expires_at": None}
            self._write(data)

    def read_list(self, key: str, limit: int = 100) -> list:
        with self._lock:
            items = (self._read().get(key) or {}).get("value") or []
        return list(items)[:limit] if isinstance(items, list) else []


def _loads(raw: Any) -> Any:
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


_store: Store | None = None


def _find_env(*suffixes: str) -> str | None:
    """Najde proměnnou podle konce názvu.

    Upstash integrace ve Vercelu umí proměnné prefixovat názvem projektu
    (``soraremlb_KV_REST_API_URL``), takže přesná shoda nestačí. Nejdřív
    zkoušíme přesný název, pak cokoli, co daným suffixem končí.
    """
    for suffix in suffixes:
        if os.environ.get(suffix):
            return os.environ[suffix]

    for suffix in suffixes:
        for key, value in os.environ.items():
            # Read-only token by nám na zápis nestačil.
            if key.endswith(suffix) and "READ_ONLY" not in key and value:
                return value
    return None


def get_store() -> Store:
    global _store
    if _store is not None:
        return _store

    url = _find_env("KV_REST_API_URL", "UPSTASH_REDIS_REST_URL")
    token = _find_env("KV_REST_API_TOKEN", "UPSTASH_REDIS_REST_TOKEN")
    _store = UpstashStore(url, token) if url and token else LocalStore()
    return _store


# --------------------------------------------------------------- klíče (jedno místo)

K_TOKEN = "sorare:token"
K_CARDS = "sorare:cards"
K_SCORES = "sorare:scores:{batch}"
K_MLB = "mlb:context:{start}:{end}"
K_JOB = "job:{job_id}"
K_LATEST_JOB = "job:latest"
K_LAST_RESULT = "result:latest"
K_SUBMITTED = "submitted:{fixture}"

# Přehledy a historie
K_OVERVIEW = "overview:{sport}"
K_UPCOMING = "sorare:upcoming"
K_RECENT_LINEUPS = "sorare:recent-lineups"
K_HIST_JOBS = "history:jobs"
K_JOB_LOCK = "job:{job_id}:lock"
K_TICK_DONE = "tick:{fixture}:{phase}"
K_TICK_REMINDER = "tick:reminder:{day}"
K_CALIBRATION = "calibration:lineups"
K_HIST_SNAPSHOTS = "history:snapshots:{sport}"
K_HIST_SNAPSHOT_DAY = "history:snapshot-day:{sport}"
