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


class Store:
    """Jednoduché key-value s TTL."""

    def get(self, key: str) -> Any | None:
        raise NotImplementedError

    def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        raise NotImplementedError

    def delete(self, key: str) -> None:
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
        LOCAL_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

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


_store: Store | None = None


def get_store() -> Store:
    global _store
    if _store is not None:
        return _store

    url = os.environ.get("KV_REST_API_URL") or os.environ.get("UPSTASH_REDIS_REST_URL")
    token = os.environ.get("KV_REST_API_TOKEN") or os.environ.get("UPSTASH_REDIS_REST_TOKEN")
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
