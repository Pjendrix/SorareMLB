"""Dotazy skládané podle aktuálního schématu Sorare.

Výsledek (hotové texty dotazů a názvy polí) se drží v Redisu 24 h,
takže SDL se stahuje nejvýš jednou denně.
"""
from __future__ import annotations

import logging
import re

from .schema import Schema, load_schema
from .store import get_store

log = logging.getLogger(__name__)

# Při každé změně toho, co build_features vrací, zvýšit — jinak by se
# 24 h používala stará uložená verze bez nových klíčů.
FEATURES_VERSION = 7
K_FEATURES = f"sorare:features:v{FEATURES_VERSION}"
REQUIRED_KEYS = ("vault", "power", "lineup_input", "rewards", "ledger", "diagnostics")
LEAF_BASES = {"Int", "Float", "String", "Boolean", "ID"}
MONEY_FIELDS = ("eurCents", "usdCents", "gbpCents", "referenceCurrency")
REWARD_LEAVES = ("quantity", "rarity", "coinAmount", "amount", "essenceAmount", "count", "xp", "points")
PLAYER_FIELDS = ("anyPlayer", "player", "baseballPlayer", "footballPlayer")
CARD_FIELDS = ("anyCard", "card")


def get_features(refresh: bool = False, schema: Schema | None = None) -> dict:
    store = get_store()
    if not refresh and schema is None:
        cached = store.get_json(K_FEATURES)
        if cached and all(k in cached for k in REQUIRED_KEYS):
            return cached
    ttl = 24 * 3600
    try:
        features = build_features(schema or load_schema(fresh=refresh))
    except Exception as exc:  # noqa: BLE001
        # Nedostupné schéma nesmí brzdit každé volání — zkusíme to za 10 minut.
        log.warning("Schéma Sorare nejde načíst: %s", exc)
        reason = f"Schéma Sorare nejde stáhnout: {type(exc).__name__}: {exc}"[:400]
        features = {
            "vault": {},
            "power": {},
            "lineup_input": {},
            "rewards": {"available": False, "reason": reason},
            "ledger": {"available": False, "reason": reason, "candidates": [], "sources": []},
            "diagnostics": {"ok": False, "error": reason},
        }
        ttl = 600
    try:
        store.set(K_FEATURES, features, ttl_seconds=ttl)
    except Exception:  # noqa: BLE001
        pass
    return features


def build_features(s: Schema) -> dict:
    return {
        "version": FEATURES_VERSION,
        "vault": _vault(s),
        "power": _power(s),
        "lineup_input": _lineup_input(s),
        "rewards": _rewards(s),
        "ledger": _ledger(s),
        "diagnostics": _diagnostics(s),
    }


DIAG_RX = r"reward|rank|account|payment|transaction|wallet|balance|deposit|withdraw|operation|activit|vault|seal"


def _diagnostics(s: Schema) -> dict:
    """Co ve schématu je — podle toho se dá doladit, když něco chybí."""
    def names(type_name: str) -> list[str]:
        return sorted(s.find(type_name, DIAG_RX))

    card_type = s.node_type("CurrentUser", "cards") or "AnyCardInterface"
    return {
        "ok": True,
        "sdl_bytes": getattr(s, "size", None),
        "types": len(s.types),
        "has_current_user": "CurrentUser" in s.types,
        "current_user_fields": len((s.types.get("CurrentUser") or TypeDefStub).fields),
        "current_user_matches": names("CurrentUser"),
        "user_matches": names("User"),
        "card_type": card_type,
        "card_matches": names(card_type),
        "query_matches": names("Query"),
    }


# ------------------------------------------------------------------ helpers


def _is_leaf(s: Schema, base: str) -> bool:
    t = s.types.get(base)
    return t is None or t.kind == "enum" or base in LEAF_BASES


def _leaves(s: Schema, type_name: str, names) -> list[str]:
    out = []
    for n in names:
        f = s.field(type_name, n)
        if f and _is_leaf(s, f.base):
            out.append(n)
    return out


def _object(s: Schema, type_name: str, name: str, inner: str) -> str:
    return f"{name} {{ {inner} }}" if inner.strip() else ""


def _player(s: Schema, type_name: str) -> str:
    name = s.pick(type_name, *PLAYER_FIELDS)
    if not name:
        return ""
    base = s.field(type_name, name).base
    leaves = _leaves(s, base, ("slug", "displayName"))
    if not leaves:
        # interface bez polí → zkusíme konkrétní typy
        return ""
    return f"playerRef: {name} {{ {' '.join(leaves)} }}"


def _card(s: Schema, type_name: str) -> str:
    name = s.pick(type_name, *CARD_FIELDS)
    if not name:
        return ""
    base = s.field(type_name, name).base
    parts = _leaves(s, base, ("slug", "rarityTyped"))
    player = _player(s, base)
    if player:
        parts.append(player)
    return f"cardRef: {name} {{ {' '.join(parts)} }}" if parts else ""


# ------------------------------------------------------------------ trezor


def _vault(s: Schema) -> dict:
    card_type = s.node_type("CurrentUser", "cards") or "AnyCardInterface"
    rx = r"vault|seal"
    direct = s.find(card_type, rx, "Boolean")
    direct.sort(key=lambda n: ("vault" not in n.lower(), len(n)))
    if direct:
        return {"card_type": card_type, "selection": f"vaultFlag: {direct[0]}", "field": direct[0]}

    # Pole může být jen na konkrétních typech karet → fragmenty.
    fragments, found = [], None
    for concrete in s.possible_types(card_type):
        names = s.find(concrete, rx, "Boolean")
        names.sort(key=lambda n: ("vault" not in n.lower(), len(n)))
        if names:
            found = found or names[0]
            fragments.append(f"... on {concrete} {{ vaultFlag: {names[0]} }}")
    return {
        "card_type": card_type,
        "selection": " ".join(fragments),
        "field": found,
    }


def _power(s: Schema) -> dict:
    """Bonus karty (power = XP + season + kolekce), pokud ho schéma vystavuje.

    Sorare ho vede jako násobitel ("1.05") nebo jako podíl (0.05). Parsování
    a normalizace je v client._power_mult.
    """
    card_type = s.node_type("CurrentUser", "cards") or "AnyCardInterface"
    rx = r"^power$|^powerBonus$|^totalBonus$|^bonus$"
    leaves = [n for n in s.find(card_type, rx) if _is_leaf(s, s.field(card_type, n).base)]
    if leaves:
        leaves.sort(key=lambda n: (n != "power", len(n)))
        return {"selection": f"cardPower: {leaves[0]}", "field": leaves[0]}
    fragments, found = [], None
    for concrete in s.possible_types(card_type):
        names = [n for n in s.find(concrete, rx) if _is_leaf(s, s.field(concrete, n).base)]
        if names:
            names.sort(key=lambda n: (n != "power", len(n)))
            found = found or names[0]
            fragments.append(f"... on {concrete} {{ cardPower: {names[0]} }}")
    return {"selection": " ".join(fragments), "field": found}


def _lineup_input(s: Schema) -> dict:
    """Jak mutaci říct, kterou existující sestavu přepsat.

    Bez ID sestavy by přepis při druhém běhu před uzávěrkou mohl skončit
    chybou „already has a lineup“ nebo druhou sestavou. Když pole ve
    schématu není, přepis se vypne a první sestava zůstane.
    """
    name = "createOrUpdateSo5LineupInput"
    if name not in s.types:
        return {"id_field": None}
    ids = [n for n in s.find(name, r"^(so5)?lineupid$|^id$")]
    ids.sort(key=lambda n: (n.lower() != "so5lineupid", len(n)))
    return {"id_field": ids[0] if ids else None, "fields": sorted(s.types[name].fields)}


# ------------------------------------------------------------------ odměny


def _rewards(s: Schema) -> dict:
    root = None
    if s.has("CurrentUser", "rewardedRankings"):
        root = ("CurrentUser", "currentUser")
    elif s.has("User", "rewardedRankings") and s.has("Query", "user"):
        root = ("User", "user(slug: $slug)")
    if not root:
        return {"available": False, "reason": "Schéma nemá rewardedRankings."}

    conn = s.field(root[0], "rewardedRankings")
    node = s.node_type(root[0], "rewardedRankings")
    parts = _leaves(s, node, ("id",))
    score = s.pick(node, "overallScore", "score")
    if score:
        parts.append(f"scoreValue: {score}")
    rank = s.pick(node, "ranking", "rank")
    if rank:
        parts.append(f"rankValue: {rank}")

    # sestava
    lineup_name = s.pick(node, "so5Lineup", "lineup")
    if lineup_name:
        lt = s.field(node, lineup_name).base
        lparts = _leaves(s, lt, ("id",))
        board_name = s.pick(lt, "so5Leaderboard", "leaderboard")
        if board_name:
            bt = s.field(lt, board_name).base
            bparts = _leaves(s, bt, ("slug", "displayName", "rarityType"))
            fx_name = s.pick(bt, "so5Fixture", "fixture")
            if fx_name:
                ft = s.field(bt, fx_name).base
                fparts = _leaves(s, ft, ("slug", "gameWeek", "sport", "startDate", "endDate"))
                if fparts:
                    bparts.append(f"fixtureRef: {fx_name} {{ {' '.join(fparts)} }}")
            lparts.append(f"boardRef: {board_name} {{ {' '.join(bparts)} }}")
        app_name = s.pick(lt, "so5Appearances", "appearances")
        if app_name:
            at = s.field(lt, app_name).base
            aparts = []
            a_score = s.pick(at, "score", "totalScore")
            if a_score and _is_leaf(s, s.field(at, a_score).base):
                aparts.append(f"scoreValue: {a_score}")
            aparts += _leaves(s, at, ("captain",))
            card = _card(s, at)
            if card:
                aparts.append(card)
            lparts.append(f"appearancesRef: {app_name} {{ {' '.join(aparts)} }}")
        parts.append(f"lineupRef: {lineup_name} {{ {' '.join(lparts)} }}")

    # odměny
    rw_name = s.pick(node, "so5Rewards", "so5Reward", "rewards")
    if rw_name:
        rt = s.field(node, rw_name).base
        rparts = _leaves(s, rt, ("id",))
        amount = s.field(rt, "amount")
        if amount and not _is_leaf(s, amount.base):
            money = _leaves(s, amount.base, MONEY_FIELDS)
            if money:
                rparts.append(f"moneyRef: amount {{ {' '.join(money)} }}")
        items_name = s.pick(rt, "rewards", "rewardItems", "items")
        if items_name:
            union = s.field(rt, items_name).base
            frags = []
            for concrete in s.possible_types(union):
                inner = []
                for leaf in REWARD_LEAVES:
                    f = s.field(concrete, leaf)
                    if not f:
                        continue
                    # Aliasy s názvem typu: jinak by GraphQL odmítl sloučit
                    # stejně pojmenovaná pole s různým typem napříč fragmenty.
                    if _is_leaf(s, f.base):
                        inner.append(f"{concrete}__{leaf}: {leaf}")
                    elif leaf == "amount":
                        money = _leaves(s, f.base, MONEY_FIELDS)
                        if money:
                            inner.append(f"{concrete}__money: amount {{ {' '.join(money)} }}")
                player = _player(s, concrete)
                if player:
                    inner.append(player.replace("playerRef:", f"{concrete}__player:", 1))
                card = _card(s, concrete)
                if card:
                    inner.append(card.replace("cardRef:", f"{concrete}__card:", 1))
                if inner:
                    frags.append(f"... on {concrete} {{ {' '.join(inner)} }}")
            rparts.append(f"itemsRef: {items_name} {{ __typename {' '.join(frags)} }}")
        parts.append(f"rewardsRef: {rw_name} {{ {' '.join(rparts)} }}")

    args = ["first: 50", "after: $after"]
    has_sport = "sport" in (conn.args or [])
    if has_sport:
        args.append("sport: $sport")
    var_defs = ["$after: String"]
    if has_sport:
        # Typ proměnné musí přesně odpovídat argumentu (včetně „!“),
        # jinak Sorare dotaz odmítne jako nevalidní.
        sport_arg_type = conn.arg_types.get("sport", "Sport")
        var_defs.append(f"$sport: {sport_arg_type}")
    if root[1].startswith("user("):
        var_defs.append("$slug: String!")

    query = (
        f"query RewardedRankings({', '.join(var_defs)}) {{ "
        f"{root[1]} {{ rewardedRankings({', '.join(args)}) {{ "
        f"pageInfo {{ hasNextPage endCursor }} nodes {{ {' '.join(parts)} }} }} }} }}"
    )
    return {
        "available": True,
        "query": query,
        "needs_slug": root[1].startswith("user("),
        "sport_arg": has_sport,
        "sport_is_list": has_sport and conn.arg_types.get("sport", "").startswith("["),
        "root_key": "currentUser" if root[1] == "currentUser" else "user",
    }


# ------------------------------------------------------------------ peníze

# Zdroje plateb a jejich role. None = typ se určí z textu záznamu.
# Varianty „…WithRates“ jsou stejná data s kurzy, proto se vynechávají.
# pendingDeposits jsou nedokončené a do bilance nepatří.
LEDGER_ROLES: dict[str, str | None] = {
    "accountEntries": None,
    "myAccountEntries": None,
    "spentFiatPaymentIntents": "card_payment",
    "withdrawals": "withdrawal",
    "bankWithdrawals": "withdrawal",
    "fastWithdrawals": "withdrawal",
    "cardShardsHistoryTransactions": "essence",
    "commonGemHistoryTransactions": "gems",
}
LEDGER_PREFERENCE = tuple(LEDGER_ROLES)
LEDGER_RX = r"accountEntr|transaction|payment|withdraw|deposit|operation|ledger|walletEntr|payout"
LEAF_RX = r"^id$|At$|date|type|kind|status|direction|description|label|reason|currency|amount|cents|wei|value|price|fee|sign|credit|debit"
MONEY_RX = r"cents|wei|amount|value"


def _money_object(s: Schema, base: str) -> list[str]:
    """Listová pole objektu, který vypadá jako částka."""
    t = s.types.get(base)
    if not t or t.kind not in ("type", "interface"):
        return []
    leaves = [f.name for f in t.fields.values() if _is_leaf(s, f.base) and re.search(MONEY_RX + "|currency", f.name, re.I)]
    return leaves if any(re.search(MONEY_RX, n, re.I) for n in leaves) else []


def _entry_selection(s: Schema, type_name: str, prefix: str = "") -> list[str]:
    t = s.types.get(type_name)
    if not t:
        return []
    out = []
    for f in t.fields.values():
        alias = f"{prefix}{f.name}: " if prefix else ""
        if f.args and any(a for a in f.args if a not in ("first", "after", "last", "before")):
            continue
        if _is_leaf(s, f.base):
            if re.search(LEAF_RX, f.name, re.I):
                out.append(f"{alias}{f.name}")
        else:
            money = _money_object(s, f.base)
            if money:
                out.append(f"{alias}{f.name} {{ {' '.join(money)} }}")
    return out


def _ledger(s: Schema) -> dict:
    user = s.types.get("CurrentUser")
    if not user:
        return {"available": False, "reason": "Schéma nemá CurrentUser.", "candidates": []}

    candidates = [
        n for n in s.find("CurrentUser", LEDGER_RX)
        if s.field("CurrentUser", n) and not _is_leaf(s, s.field("CurrentUser", n).base)
    ]
    candidates.sort(key=lambda n: (LEDGER_PREFERENCE.index(n) if n in LEDGER_PREFERENCE else 99, n))

    sources = []
    skipped = []
    for name in candidates:
        if name not in LEDGER_ROLES:
            continue
        fdef = s.field("CurrentUser", name)
        required = [
            a for a, t in fdef.arg_types.items()
            if t.endswith("!") and a not in ("first", "after", "last", "before")
        ]
        if required:
            skipped.append(f"{name} (povinné argumenty: {', '.join(required)})")
            continue
        node = s.node_type("CurrentUser", name)
        is_conn = s.has(fdef.base, "nodes")
        kind = (s.types.get(node) or TypeDefStub).kind
        if kind in ("union", "interface"):
            fields = ["__typename"]
            fields += _entry_selection(s, node) if kind == "interface" else []
            for concrete in s.possible_types(node):
                inner = _entry_selection(s, concrete, prefix=f"{concrete}__")
                if inner:
                    fields.append(f"... on {concrete} {{ {' '.join(inner)} }}")
        else:
            fields = _entry_selection(s, node)
        if len(fields) < 2:
            skipped.append(f"{name} (žádná použitelná pole)")
            continue
        args = []
        var_defs = []
        if is_conn:
            if "first" in fdef.args:
                args.append("first: 50")
            if "after" in fdef.args:
                args.append("after: $after")
                var_defs.append("$after: String")
        arg_text = f"({', '.join(args)})" if args else ""
        var_text = f"({', '.join(var_defs)})" if var_defs else ""
        body = (
            f"pageInfo {{ hasNextPage endCursor }} nodes {{ {' '.join(fields)} }}"
            if is_conn else " ".join(fields)
        )
        op = f"Ledger{name[0].upper()}{name[1:]}"
        sources.append({
            "field": name,
            "role": LEDGER_ROLES[name],
            "paginated": is_conn and "after" in fdef.args,
            "connection": is_conn,
            "operation": op,
            "query": f"query {op}{var_text} {{ currentUser {{ {name}{arg_text} {{ {body} }} }} }}",
        })

    # zůstatky
    balance_parts = []
    for f in user.fields.values():
        if f.args:
            continue
        if re.search(r"balance", f.name, re.I):
            if _is_leaf(s, f.base):
                balance_parts.append(f.name)
            else:
                money = _money_object(s, f.base)
                if money:
                    balance_parts.append(f"{f.name} {{ {' '.join(money)} }}")
        elif re.search(r"wallet|account", f.name, re.I) and not _is_leaf(s, f.base):
            inner = [
                x.name for x in (s.types.get(f.base) or TypeDefStub).fields.values()
                if re.search(r"balance", x.name, re.I) and _is_leaf(s, x.base) and not x.args
            ]
            if inner:
                balance_parts.append(f"{f.name} {{ {' '.join(inner)} }}")
    balance_query = (
        f"query Balances {{ currentUser {{ {' '.join(balance_parts)} }} }}" if balance_parts else None
    )

    return {
        "available": bool(sources),
        "reason": None if sources else "Ve schématu jsem nenašel historii plateb.",
        "candidates": candidates,
        "skipped": skipped,
        "sources": sources,
        "balance_query": balance_query,
    }


class TypeDefStub:
    kind = ""
    fields: dict = {}
