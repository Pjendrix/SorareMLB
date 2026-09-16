"""Dotazy skládané podle aktuálního schématu Sorare.

Výsledek (hotové texty dotazů a názvy polí) se drží v Redisu 24 h,
takže SDL se stahuje nejvýš jednou denně.
"""
from __future__ import annotations

import logging

from .schema import Schema, load_schema
from .store import get_store

log = logging.getLogger(__name__)

K_FEATURES = "sorare:features:v1"
LEAF_BASES = {"Int", "Float", "String", "Boolean", "ID"}
MONEY_FIELDS = ("eurCents", "usdCents", "gbpCents", "referenceCurrency")
REWARD_LEAVES = ("quantity", "rarity", "coinAmount", "amount", "essenceAmount", "count", "xp", "points")
PLAYER_FIELDS = ("anyPlayer", "player", "baseballPlayer", "footballPlayer")
CARD_FIELDS = ("anyCard", "card")


def get_features(refresh: bool = False, schema: Schema | None = None) -> dict:
    store = get_store()
    if not refresh and schema is None:
        cached = store.get_json(K_FEATURES)
        if cached:
            return cached
    ttl = 24 * 3600
    try:
        features = build_features(schema or load_schema())
    except Exception as exc:  # noqa: BLE001
        # Nedostupné schéma nesmí brzdit každé volání — zkusíme to za hodinu.
        log.warning("Schéma Sorare nejde načíst: %s", exc)
        features = {
            "vault": {},
            "rewards": {"available": False, "reason": f"Schéma nejde stáhnout: {exc}"[:300]},
        }
        ttl = 3600
    try:
        store.set(K_FEATURES, features, ttl_seconds=ttl)
    except Exception:  # noqa: BLE001
        pass
    return features


def build_features(s: Schema) -> dict:
    return {
        "vault": _vault(s),
        "rewards": _rewards(s),
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
        sport_arg_type = conn.arg_types.get("sport", "Sport").rstrip("!")
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
