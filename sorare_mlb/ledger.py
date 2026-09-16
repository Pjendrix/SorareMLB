"""Bilance účtu: vklady, výběry, nákupy, prodeje, poplatky.

Sorare historii plateb dokumentuje jen útržkovitě, proto se zdroj hledá
ve schématu (`features._ledger`) a záznamy se třídí podle textu typu.
Když API něco nevrátí (nebo když platíš kartou mimo Sorare peněženku),
dají se doplnit ruční záznamy.
"""
from __future__ import annotations

import re
import time
import uuid
from collections import defaultdict
from datetime import date

from .archive import Archive, paginate
from .client import SorareClient
from .features import get_features
from .models import Config
from .store import get_store

ARCHIVE = Archive("ledger", "date")
K_MANUAL = "ledger:manual"
K_BALANCE = "ledger:balance"
K_SAMPLE = "ledger:sample"

CATEGORIES = {
    "deposit": "Vklad",
    "card_payment": "Platba kartou",
    "withdrawal": "Výběr",
    "purchase": "Nákup karet",
    "sale": "Prodej karet",
    "reward": "Výhra",
    "fee": "Poplatek",
    "refund": "Vrácení",
    "other": "Ostatní",
    "essence": "Essence",
    "gems": "Gemy",
}
# Kategorie, které nejsou peníze, ale množství.
QUANTITY = ("essence", "gems")

RULES = [
    ("refund", r"refund|cancel|revert|chargeback"),
    ("fee", r"fee|commission|gas"),
    ("withdrawal", r"withdraw|payout|cash_?out|transfer_?out"),
    ("deposit", r"deposit|top_?up|funding|pay_?in|transfer_?in|credit_?card_?payment"),
    ("sale", r"sale|sell|sold|offer_?accepted_?by_?buyer"),
    ("purchase", r"buy|bought|purchase|bid|auction|pack|shop|acqui|primary|offer"),
    ("reward", r"reward|prize|referral|bonus"),
]


def classify(text: str, amount: float) -> str:
    t = text.lower()
    for category, rx in RULES:
        if re.search(rx, t):
            return category
    return "other"


# ------------------------------------------------------------------ normalizace


def _flatten(node: dict, prefix: str = "") -> dict:
    out = {}
    for key, value in node.items():
        name = key.split("__", 1)[-1]
        if isinstance(value, dict):
            for k, v in _flatten(value).items():
                out[f"{name}.{k}"] = v
        else:
            out[name] = value
    return out


def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize(node: dict, source: str, role: str | None = None) -> dict:
    flat = _flatten(node)
    date_value = next(
        (v for k, v in flat.items() if re.search(r"At$|date", k.split(".")[-1], re.I) and v), None
    )
    type_text = " ".join(
        str(v) for k, v in flat.items()
        if re.search(r"typename|type|kind|direction|reason|label|description", k, re.I) and v
    )

    eur = usd = eth = None
    for k, v in flat.items():
        leaf = k.split(".")[-1].lower()
        if leaf == "eurcents":
            eur = (eur or 0) + (_num(v) or 0) / 100
        elif leaf == "usdcents":
            usd = (usd or 0) + (_num(v) or 0) / 100
        elif leaf == "wei":
            eth = (eth or 0) + (_num(v) or 0) / 1e18
    if eur is None and usd is None and eth is None:
        currency = str(next((v for k, v in flat.items() if "currency" in k.lower()), "") or "").upper()
        amount = next(
            (_num(v) for k, v in flat.items()
             if re.search(r"amount|value|price", k, re.I) and _num(v) is not None), None
        )
        if amount is not None:
            if "cents" in type_text.lower() or any("cents" in k.lower() for k in flat):
                amount /= 100
            if currency in ("USD",):
                usd = amount
            elif currency in ("ETH", "WEI"):
                eth = amount / 1e18 if currency == "WEI" else amount
            else:
                eur = amount

    sign_hint = " ".join(str(v) for k, v in flat.items() if re.search(r"sign|direction|debit|credit", k, re.I))
    category = role or classify(type_text + " " + sign_hint, eur or usd or eth or 0)

    qty = None
    if category in QUANTITY:
        # Essence a gemy: množství se znaménkem (+ získáno, − utraceno).
        qty = next(
            (_num(v) for k, v in flat.items()
             if re.search(r"quantity|amount|count|delta|value", k.split(".")[-1], re.I)
             and _num(v) is not None),
            None,
        )
        if qty is not None and re.search(r"spend|spent|burn|debit|used|consum|out", type_text + " " + sign_hint, re.I):
            qty = -abs(qty)
        eur = usd = eth = None

    return {
        "id": f"{source}:{flat.get('id') or uuid.uuid5(uuid.NAMESPACE_OID, str(sorted(flat.items())))}",
        "source": source,
        "date": str(date_value or "")[:19] or None,
        "type": type_text.strip()[:80] or None,
        "category": category,
        "eur": _abs(eur),
        "usd": _abs(usd),
        "eth": _abs(eth),
        "status": flat.get("status"),
        "role": role,
        "qty": qty,
        "rarity": str(flat.get("rarity") or "").lower() or None,
    }


def _abs(value):
    return None if value is None else round(abs(value), 6)


# ------------------------------------------------------------------ stažení


def sync(config: Config, full: bool = False) -> dict:
    feats = get_features().get("ledger") or {}
    if not feats.get("available"):
        raise RuntimeError(feats.get("reason") or "Historie plateb není ve schématu k dispozici.")
    client = SorareClient(config)
    budget = float(config.get_path("rewards.budget_seconds", 40))
    started = time.monotonic()
    results = []

    for src in feats["sources"]:
        def fetch_page(cursor, src=src):
            variables = {"after": cursor} if src["paginated"] else {}
            body = client.execute(src["query"], variables, operation_name=src["operation"], tolerate_errors=True)
            if body.get("errors") and not body.get("data"):
                raise RuntimeError("; ".join(e.get("message", "?") for e in body["errors"])[:300])
            block = ((body.get("data") or {}).get("currentUser") or {}).get(src["field"])
            if isinstance(block, list):
                nodes, next_cursor = block, None
            elif src["connection"]:
                nodes = (block or {}).get("nodes") or []
                page = (block or {}).get("pageInfo") or {}
                next_cursor = page.get("endCursor") if page.get("hasNextPage") and src["paginated"] else None
            else:
                nodes, next_cursor = [block] if block else [], None
            if nodes and cursor is None:
                get_store().set(f"{K_SAMPLE}:{src['field']}", nodes[:3], ttl_seconds=7 * 24 * 3600)
            return [normalize(n, src["field"], src.get("role")) for n in nodes], next_cursor

        remaining = budget - (time.monotonic() - started)
        if full and remaining <= 3:
            results.append({"scope": src["field"], "done": False, "fetched": 0, "new": 0, "pages": 0})
            continue
        try:
            results.append(paginate(fetch_page, ARCHIVE, src["field"], full, remaining, 6))
        except Exception as exc:  # noqa: BLE001 — jeden zdroj nesmí shodit ostatní
            results.append({"scope": src["field"], "done": True, "fetched": 0, "new": 0,
                            "pages": 0, "error": f"{type(exc).__name__}: {exc}"[:300]})

    if feats.get("balance_query"):
        try:
            body = client.execute(feats["balance_query"], operation_name="Balances", tolerate_errors=True)
            balances = _flatten((body.get("data") or {}).get("currentUser") or {})
            get_store().set(K_BALANCE, {"at": date.today().isoformat(), "values": balances})
        except Exception:  # noqa: BLE001
            pass

    return {
        "done": all(r["done"] for r in results),
        "fetched": sum(r["fetched"] for r in results),
        "new": sum(r["new"] for r in results),
        "scopes": results,
    }


def reset_backfill() -> None:
    feats = get_features().get("ledger") or {}
    for src in feats.get("sources") or []:
        ARCHIVE.reset(src["field"])


def samples() -> dict:
    feats = get_features().get("ledger") or {}
    return {
        src["field"]: get_store().get_json(f"{K_SAMPLE}:{src['field']}")
        for src in feats.get("sources") or []
    }


# ------------------------------------------------------------------ ruční záznamy


def manual_entries() -> list[dict]:
    return get_store().get_json(K_MANUAL, []) or []


def add_manual(category: str, eur: float, when: str, note: str = "") -> dict:
    if category not in CATEGORIES:
        raise ValueError(f"Neznámá kategorie {category}.")
    entry = {
        "id": "manual:" + uuid.uuid4().hex[:10],
        "source": "manual",
        "date": str(date.fromisoformat(when)),
        "type": note[:80] or CATEGORIES[category],
        "category": category,
        "eur": round(abs(float(eur)), 2),
        "usd": None,
        "eth": None,
        "status": None,
    }
    items = manual_entries()
    items.append(entry)
    get_store().set(K_MANUAL, items)
    return entry


def delete_manual(entry_id: str) -> bool:
    items = manual_entries()
    kept = [e for e in items if e["id"] != entry_id]
    get_store().set(K_MANUAL, kept)
    return len(kept) != len(items)


# ------------------------------------------------------------------ souhrn


def summarize(rows: list[dict], reward_money_eur: float = 0.0, exclude_failed: bool = True) -> dict:
    if exclude_failed:
        rows = [
            r for r in rows
            if not re.search(r"fail|cancel|reject|pending|processing|expired", str(r.get("status") or ""), re.I)
        ]

    # Výběry mohou být v obecném výpisu účtu i ve specializovaném zdroji.
    # Specializovaný zdroj se započítá jen tehdy, když obecný tu kategorii nemá.
    general = {r["category"] for r in rows if r.get("source") not in ("manual",) and not r.get("role")}
    counted, duplicates = [], 0
    for r in rows:
        if r.get("role") and r["role"] in general:
            duplicates += 1
            continue
        counted.append(r)
    rows = counted

    totals = {c: {"eur": 0.0, "usd": 0.0, "eth": 0.0, "count": 0} for c in CATEGORIES}
    quantities: dict[str, dict] = {c: {"gained": 0.0, "spent": 0.0, "count": 0} for c in QUANTITY}
    by_year: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    by_month: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    for r in rows:
        cat = r["category"]
        if cat in QUANTITY:
            q = quantities[cat]
            q["count"] += 1
            if (r.get("qty") or 0) >= 0:
                q["gained"] += r.get("qty") or 0
            else:
                q["spent"] += -(r.get("qty") or 0)
            continue
        t = totals[cat]
        t["count"] += 1
        for cur in ("eur", "usd", "eth"):
            t[cur] += r.get(cur) or 0
        period = str(r.get("date") or "")
        if len(period) >= 7:
            by_year[period[:4]][cat] += r.get("eur") or r.get("usd") or 0
            by_month[period[:7]][cat] += r.get("eur") or r.get("usd") or 0

    def eur(c):
        return round(totals[c]["eur"] + totals[c]["usd"], 2)  # USD bereme 1:1 jen pro hrubý přehled

    invested = eur("deposit") + eur("card_payment")
    balance = get_store().get_json(K_BALANCE)
    headline = {
        "deposited": eur("deposit"),
        "card_payments": eur("card_payment"),
        "invested": round(invested, 2),
        "withdrawn": eur("withdrawal"),
        "spent": round(eur("purchase") + eur("card_payment"), 2),
        "sold": eur("sale"),
        "fees": eur("fee"),
        "refunds": eur("refund"),
        "rewards_money": round(reward_money_eur, 2),
        "cash_result": round(eur("withdrawal") - invested, 2),
        "market_result": round(
            eur("sale") - eur("purchase") - eur("card_payment") - eur("fee") + eur("refund"), 2
        ),
        "has_usd": any(totals[c]["usd"] for c in totals),
        "eth": {c: round(totals[c]["eth"], 6) for c in totals if totals[c]["eth"]},
        "skipped_duplicates": duplicates,
    }
    return {
        "headline": headline,
        "quantities": {k: {kk: round(vv, 2) if kk != "count" else vv for kk, vv in v.items()}
                       for k, v in quantities.items()},
        "categories": [
            {"key": c, "label": CATEGORIES[c],
             **{k: (round(v, 6) if k == "eth" else round(v, 2)) if k != "count" else v
                for k, v in totals[c].items()}}
            for c in CATEGORIES if c not in QUANTITY
        ],
        "years": [{"year": y, **{k: round(v, 2) for k, v in d.items()}} for y, d in sorted(by_year.items())],
        "months": [{"month": m, **{k: round(v, 2) for k, v in d.items()}} for m, d in sorted(by_month.items())],
        "balance": balance,
        "entries": rows[:300],
        "count": len(rows),
        "first_date": min((r["date"] for r in rows if r.get("date")), default=None),
    }
