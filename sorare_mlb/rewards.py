"""Výhry: co přinesly soutěže, gameweeky, hráči a jednotlivé karty.

Zdrojem je `rewardedRankings` (umístění, za která přišla odměna). Sorare
u nich nevrací všechno — podle issue #670 v sorare/api chybí např. odměny
za streaky. Stažená umístění se ukládají do archivu v Redisu (po čtvrtletích, bez
limitu) a přehled se počítá z archivu. Celou historii účtu stáhne
`sync(full=True)` po dávkách.

Přiřazení odměn kartám:
* když položka odměny nese hráče (typicky Essence), jde přesně k němu,
* jinak se odměna sestavy rozpočítá mezi karty podle jejich podílu na bodech
  sestavy (v UI označeno jako odhad).
"""
from __future__ import annotations

import time
from collections import defaultdict

from .archive import Archive, paginate
from .auth import token_status
from .client import SorareClient
from .features import get_features
from .models import Config
from .store import get_store

SPORT_ENUM = {"mlb": "BASEBALL", "football": "FOOTBALL"}
ENUM_SPORT = {v: k for k, v in SPORT_ENUM.items()}
ARCHIVE = Archive("rewards", "end")


# ------------------------------------------------------------------ stažení


def sync(config: Config, sport: str | None = None, full: bool = False) -> dict:
    """Stáhne umístění s odměnou do archivu.

    `full=True` stahuje celou historii účtu po dávkách (navazuje kurzorem);
    volá se opakovaně, dokud výsledek nemá `done: true`.
    """
    feats = get_features().get("rewards") or {}
    if not feats.get("available"):
        raise RuntimeError(feats.get("reason") or "Odměny nejsou ve schématu k dispozici.")

    client = SorareClient(config)
    sports = [sport] if sport else list(SPORT_ENUM)
    if not feats.get("sport_arg"):
        sports = [None]
    budget = float(config.get_path("rewards.budget_seconds", 40))
    max_pages = int(config.get_path("rewards.max_pages", 6))

    results = []
    started = time.monotonic()
    for sp in sports:
        def fetch_page(cursor, sp=sp):
            variables: dict = {"after": cursor}
            if sp and feats.get("sport_arg"):
                variables["sport"] = [SPORT_ENUM[sp]] if feats.get("sport_is_list") else SPORT_ENUM[sp]
            if feats.get("needs_slug"):
                variables["slug"] = token_status().get("user_slug")
            body = client.execute(feats["query"], variables, operation_name="RewardedRankings")
            block = ((body.get("data") or {}).get(feats["root_key"]) or {}).get("rewardedRankings") or {}
            rows = [normalize(raw, default_sport=sp) for raw in block.get("nodes") or []]
            page = block.get("pageInfo") or {}
            return [r for r in rows if r["id"]], page.get("endCursor") if page.get("hasNextPage") else None

        remaining = budget - (time.monotonic() - started)
        if full and remaining <= 3:
            results.append({"scope": sp or "all", "done": False, "fetched": 0, "new": 0, "pages": 0})
            continue
        results.append(paginate(fetch_page, ARCHIVE, sp or "all", full, remaining, max_pages))

    return {
        "done": all(r["done"] for r in results),
        "fetched": sum(r["fetched"] for r in results),
        "new": sum(r["new"] for r in results),
        "scopes": results,
    }


def reset_backfill() -> None:
    for scope in [*SPORT_ENUM, "all"]:
        ARCHIVE.reset(scope)


def backfill_status() -> dict:
    return {scope: ARCHIVE.cursor_state(scope) for scope in [*SPORT_ENUM, "all"]}


def archive(sport: str | None = None) -> list[dict]:
    rows = ARCHIVE.rows()
    return [r for r in rows if not sport or r["sport"] == sport]


def merge(rows: list[dict]) -> int:
    return ARCHIVE.merge(rows)


# ------------------------------------------------------------------ normalizace


def _money(ref: dict | None) -> tuple[float, float]:
    ref = ref or {}
    return (ref.get("eurCents") or 0) / 100, (ref.get("usdCents") or 0) / 100


def _kind(typename: str) -> str:
    t = typename.lower()
    if "shard" in t or "essence" in t:
        return "essence"
    if "monetary" in t or "cash" in t:
        return "money"
    if "coin" in t or "currency" in t:
        return "coins"
    if "pack" in t:
        return "pack"
    if "card" in t:
        return "card"
    if "experience" in t or "xp" in t:
        return "xp"
    return "other"


def _field(item: dict, suffix: str):
    for key, value in item.items():
        if key.endswith("__" + suffix):
            return value
    return None


def normalize(raw: dict, default_sport: str | None = None) -> dict:
    lineup = raw.get("lineupRef") or {}
    board = lineup.get("boardRef") or {}
    fixture = board.get("fixtureRef") or {}
    sport = ENUM_SPORT.get(str(fixture.get("sport") or "").upper(), default_sport or "?")

    cards = []
    for app in lineup.get("appearancesRef") or []:
        card = app.get("cardRef") or {}
        player = card.get("playerRef") or {}
        cards.append(
            {
                "slug": card.get("slug"),
                "rarity": str(card.get("rarityTyped") or "").lower() or None,
                "player": player.get("displayName") or card.get("slug"),
                "player_slug": player.get("slug"),
                "score": float(app.get("scoreValue") or 0),
                "captain": bool(app.get("captain")),
            }
        )

    items = []
    for reward in raw.get("rewardsRef") or []:
        eur, usd = _money(reward.get("moneyRef"))
        sub = reward.get("itemsRef") or []
        if (eur or usd) and not any(_kind(i.get("__typename", "")) == "money" for i in sub):
            items.append({"kind": "money", "eur": eur, "usd": usd})
        for item in sub:
            kind = _kind(item.get("__typename", ""))
            i_eur, i_usd = _money(_field(item, "money"))
            player = _field(item, "player") or {}
            card = _field(item, "card") or {}
            qty = _field(item, "quantity") or _field(item, "count") or _field(item, "essenceAmount")
            amount = _field(item, "amount")
            items.append(
                {
                    "kind": kind,
                    "type": item.get("__typename"),
                    "rarity": str(_field(item, "rarity") or card.get("rarityTyped") or "").lower() or None,
                    "qty": float(qty if qty is not None else (amount if isinstance(amount, (int, float)) else 1)),
                    "coins": float(_field(item, "coinAmount") or 0),
                    "eur": i_eur,
                    "usd": i_usd,
                    "player": player.get("displayName") or (card.get("playerRef") or {}).get("displayName"),
                    "player_slug": player.get("slug") or (card.get("playerRef") or {}).get("slug"),
                    "card": card.get("slug"),
                }
            )

    return {
        "id": raw.get("id"),
        "sport": sport,
        "game_week": fixture.get("gameWeek"),
        "end": fixture.get("endDate"),
        "tournament": board.get("displayName") or board.get("slug"),
        "board_slug": board.get("slug"),
        "rarity": str(board.get("rarityType") or "").lower() or None,
        "score": raw.get("scoreValue"),
        "ranking": raw.get("rankValue"),
        "cards": cards,
        "items": items,
    }


# ------------------------------------------------------------------ agregace


def _blank() -> dict:
    return {"money": 0.0, "essence": 0.0, "cards": 0, "coins": 0.0, "count": 0}


def summarize(rows: list[dict], sport: str | None = None, rarity_filter: list[str] | None = None) -> dict:
    rows = [
        r for r in rows
        if (not sport or r["sport"] == sport)
        and (not rarity_filter or not r.get("rarity") or r["rarity"] in rarity_filter)
    ]
    totals = _blank()
    essence_by_rarity: dict[str, float] = defaultdict(float)
    by_comp: dict[str, dict] = defaultdict(lambda: {**_blank(), "best": None, "scores": []})
    by_gw: dict = defaultdict(_blank)
    by_card: dict[str, dict] = {}
    by_player: dict[str, dict] = {}
    exact_essence = 0.0

    def card_entry(c: dict) -> dict:
        key = c.get("slug") or c.get("player")
        return by_card.setdefault(key, {
            "slug": c.get("slug"), "player": c.get("player"), "rarity": c.get("rarity"),
            "appearances": 0, "points": 0.0, "money": 0.0, "essence": 0.0, "essence_est": 0.0,
        })

    def player_entry(name: str | None, slug: str | None) -> dict:
        key = slug or name or "?"
        return by_player.setdefault(key, {
            "player": name or slug or "?", "appearances": 0, "points": 0.0,
            "money": 0.0, "essence": 0.0, "essence_est": 0.0,
        })

    for r in rows:
        money = sum(i["eur"] or i["usd"] for i in r["items"] if i["kind"] == "money")
        cards_won = sum(1 for i in r["items"] if i["kind"] in ("card", "pack"))
        coins = sum(i["coins"] or (i["qty"] if i["kind"] == "coins" else 0) for i in r["items"] if i["kind"] == "coins")
        essence_items = [i for i in r["items"] if i["kind"] == "essence"]
        essence = sum(i["qty"] for i in essence_items)

        for bucket in (totals, by_comp[r["tournament"] or "?"], by_gw[r["game_week"]]):
            bucket["money"] += money
            bucket["essence"] += essence
            bucket["cards"] += cards_won
            bucket["coins"] += coins
            bucket["count"] += 1
        comp = by_comp[r["tournament"] or "?"]
        if r.get("ranking") and (comp["best"] is None or r["ranking"] < comp["best"]):
            comp["best"] = r["ranking"]
        if r.get("score") is not None:
            comp["scores"].append(float(r["score"]))
        for i in essence_items:
            essence_by_rarity[i["rarity"] or "?"] += i["qty"]

        lineup_points = sum(c["score"] for c in r["cards"]) or 0.0
        unassigned_essence = 0.0
        for i in essence_items:
            if i.get("player") or i.get("player_slug"):
                p = player_entry(i.get("player"), i.get("player_slug"))
                p["essence"] += i["qty"]
                exact_essence += i["qty"]
                for c in r["cards"]:
                    if c.get("player_slug") and c["player_slug"] == i.get("player_slug"):
                        card_entry(c)["essence"] += i["qty"]
                        break
            else:
                unassigned_essence += i["qty"]

        for c in r["cards"]:
            share = (c["score"] / lineup_points) if lineup_points else (1 / max(len(r["cards"]), 1))
            ce = card_entry(c)
            pe = player_entry(c.get("player"), c.get("player_slug"))
            for e in (ce, pe):
                e["appearances"] += 1
                e["points"] += c["score"]
                e["money"] += money * share
                e["essence_est"] += unassigned_essence * share

    def finish(d: dict) -> dict:
        for v in d.values():
            for k in ("money", "essence", "essence_est", "points"):
                if k in v:
                    v[k] = round(v[k], 2)
            v["essence_total"] = round(v.get("essence", 0) + v.get("essence_est", 0), 2)
        return d

    comps = []
    for name, v in by_comp.items():
        scores = v.pop("scores")
        comps.append({"tournament": name, **v, "avg_score": round(sum(scores) / len(scores), 1) if scores else None})

    return {
        "sport": sport,
        "count": len(rows),
        "totals": {**totals, "money": round(totals["money"], 2)},
        "essence_by_rarity": dict(essence_by_rarity),
        "essence_exact_share": round(exact_essence / totals["essence"], 2) if totals["essence"] else None,
        "competitions": sorted(comps, key=lambda c: (-c["money"], -c["essence"], -c["count"])),
        "game_weeks": [
            {"game_week": gw, **v} for gw, v in sorted(by_gw.items(), key=lambda kv: (kv[0] is None, kv[0] or 0))
        ],
        "cards": sorted(finish(by_card).values(), key=lambda c: (-c["essence_total"], -c["money"], -c["points"]))[:100],
        "players": sorted(finish(by_player).values(), key=lambda c: (-c["essence_total"], -c["money"], -c["points"]))[:60],
        "recent": rows[:30],
    }
