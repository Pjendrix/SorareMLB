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

# „ledger2“: záznamy z první verze měly chybně přečtené částky a data,
# nový prefix je zahodí a historie se stáhne znovu.
ARCHIVE = Archive("ledger2", "date")
K_MANUAL = "ledger:manual"
K_BALANCE = "ledger:balance"
K_SAMPLE = "ledger:sample"

CATEGORIES = {
    "deposit": "Vklad",
    "card_payment": "Platba kartou",
    "withdrawal": "Výběr",
    "purchase": "Nákup karet",
    "pack": "Balíček za gemy",
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
    ("reward", r"reward|prize|referral|bonus|mission|quest|achievement|cashback|streak"),
    ("sale", r"sale|sell|sold|offer_?accepted_?by_?buyer"),
    ("purchase", r"buy|bought|purchase|bid|auction|pack|shop|acqui|primary|offer"),
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


DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}")
DATE_KEY_RX = re.compile(r"(At|Date|^date|^time|timestamp)$", re.I)
TYPE_KEY_RX = re.compile(r"typename|type$|kind|direction|reason|label|description|title|name$", re.I)
IN_GAME_RX = re.compile(r"gem|coin|in_?game|shard|essence|credit", re.I)
FIAT = {"EUR": "eur", "USD": "usd", "GBP": "gbp"}


def _date(flat: dict) -> str | None:
    # Jen skutečná data — pole jako `isUpdatable` obsahují „date“, ale nesou True/False.
    candidates = [
        (k, v) for k, v in flat.items()
        if isinstance(v, str) and DATE_RX.match(v) and DATE_KEY_RX.search(k.split(".")[-1])
    ]
    candidates.sort(key=lambda kv: (not re.search(r"created|occurred|paid|executed", kv[0], re.I), kv[0]))
    return candidates[0][1][:19] if candidates else None


def _money_groups(flat: dict) -> dict[str, dict]:
    """Seskupí listová pole podle objektu (`amounts.eurCents` → skupina `amounts`)."""
    groups: dict[str, dict] = defaultdict(dict)
    for k, v in flat.items():
        prefix, _, leaf = k.rpartition(".")
        groups[prefix][leaf] = v
    return groups


def _amounts(flat: dict, type_text: str) -> dict:
    """Vrátí {eur, usd, eth, in_game, currency} pro jeden záznam.

    Sorare u částky vrací ekvivalent ve více měnách (eurCents, usdCents,
    wei) a `referenceCurrency` říká, ve které se opravdu platilo. Do součtů
    v eurech jde vždy eurový ekvivalent — nesčítá se EUR + USD.
    """
    out = {"eur": None, "usd": None, "eth": None, "in_game": None, "currency": None}
    for prefix, g in _money_groups(flat).items():
        keys = {k.lower(): k for k in g}
        if not any(x in keys for x in ("eurcents", "usdcents", "gbpcents", "wei")):
            continue
        ref = str(g.get(keys.get("referencecurrency", ""), "") or "").upper() or None
        if out["eur"] is None and _num(g.get(keys.get("eurcents"))) is not None:
            out["eur"] = _num(g[keys["eurcents"]]) / 100
        if out["usd"] is None and _num(g.get(keys.get("usdcents"))) is not None:
            out["usd"] = _num(g[keys["usdcents"]]) / 100
        if _num(g.get(keys.get("wei"))) is not None and (ref in (None, "ETH", "WEI") or out["eur"] is None):
            out["eth"] = _num(g[keys["wei"]]) / 1e18
        out["currency"] = out["currency"] or ref
    if any(out[k] is not None for k in ("eur", "usd", "eth")):
        if out["currency"] and out["currency"] not in ("ETH", "WEI"):
            out["eth"] = None                     # wei je jen přepočet
        return out

    # Volná částka bez objektu (např. platební záměry).
    currency = str(next(
        (v for k, v in flat.items() if re.search(r"currency|unit|method", k, re.I) and v), ""
    ) or "").upper()
    amount_key, amount = next(
        ((k, _num(v)) for k, v in flat.items()
         if re.search(r"amount|value|price|total", k.split(".")[-1], re.I)
         and not isinstance(v, bool) and _num(v) is not None),
        (None, None),
    )
    if amount is None:
        return out
    out["currency"] = currency or None
    if IN_GAME_RX.search(currency):
        out["in_game"] = amount
    elif amount_key and "wei" in amount_key.lower() or currency in ("ETH", "WEI") or abs(amount) >= 1e12:
        # Celá čísla řádu 10^15+ jsou wei (1 ETH = 10^18 wei).
        out["eth"] = amount / 1e18 if abs(amount) >= 1e9 else amount
    else:
        if amount_key and "cents" in amount_key.lower():
            amount /= 100
        out[FIAT.get(currency, "eur")] = amount
    return out


def normalize(node: dict, source: str, role: str | None = None) -> dict:
    flat = _flatten(node)
    type_text = " ".join(
        str(v) for k, v in flat.items()
        if TYPE_KEY_RX.search(k.split(".")[-1]) and isinstance(v, str) and v
    ).strip()
    sign_hint = " ".join(
        str(v) for k, v in flat.items()
        if re.search(r"sign|direction|debit|credit", k, re.I) and not isinstance(v, bool)
    )
    money = _amounts(flat, type_text)
    category = role or classify(type_text + " " + sign_hint, 0)

    qty = None
    if category in QUANTITY:
        # Essence a gemy: množství se znaménkem (+ získáno, − utraceno).
        qty = next(
            (_num(v) for k, v in flat.items()
             if re.search(r"quantity|amount|count|delta|value", k.split(".")[-1], re.I)
             and not isinstance(v, bool) and _num(v) is not None),
            None,
        )
        if qty is not None and re.search(r"spend|spent|burn|debit|used|consum|out", type_text + " " + sign_hint, re.I):
            qty = -abs(qty)
        money = {"eur": None, "usd": None, "eth": None, "in_game": None, "currency": None}
    elif category == "purchase" and (
        IN_GAME_RX.search(str(money["currency"] or "")) or re.search(r"^pack\b", type_text, re.I)
    ):
        # Balíčky se kupují za gemy — v eurech je jen orientační ekvivalent,
        # peníze za gemy už jsou v platbách kartou / vkladech.
        category = "pack"

    return {
        "id": f"{source}:{flat.get('id') or uuid.uuid5(uuid.NAMESPACE_OID, str(sorted(flat.items())))}",
        "source": source,
        "date": _date(flat),
        "type": type_text[:80] or None,
        "category": category,
        "eur": _abs(money["eur"]),
        "usd": _abs(money["usd"]),
        "eth": _abs(money["eth"]),
        "in_game": _abs(money["in_game"]),
        "currency": money["currency"],
        "status": flat.get("status"),
        "role": role,
        "qty": qty,
        "rarity": str(flat.get("rarity") or "").lower() or None,
    }


def _eur_value(r: dict) -> float:
    """Eurová hodnota záznamu; USD jen když eurový ekvivalent chybí (1:1)."""
    if r.get("eur") is not None:
        return r["eur"]
    return r.get("usd") or 0.0


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
        value = _eur_value(r)
        t["eur"] += value
        t["usd"] += 0 if r.get("eur") is not None else (r.get("usd") or 0)
        if r.get("eur") is None and r.get("usd") is None:
            t["eth"] += r.get("eth") or 0
        t["in_game"] = t.get("in_game", 0) + (r.get("in_game") or 0)
        period = str(r.get("date") or "")
        if len(period) >= 7:
            by_year[period[:4]][cat] += value
            by_month[period[:7]][cat] += value

    def eur(c):
        return round(totals[c]["eur"], 2)

    def eth(c):
        return round(totals[c]["eth"], 6)

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
        # Částky jen v ETH (bez eurového ekvivalentu) — do eur se nepřepočítávají.
        "eth": {c: eth(c) for c in totals if totals[c]["eth"]},
        "card_payments_eth": eth("card_payment"),
        "packs_eur_equiv": eur("pack"),
        "packs_count": totals["pack"]["count"],
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
