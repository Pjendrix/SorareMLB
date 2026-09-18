"""MLB StatsAPI — veřejné, zdarma, bez klíče.

Doplňuje to, co Sorare nedává:
* kdo vůbec hraje (rozpis na daný den),
* probable pitchery,
* K% a ERA nadhazovačů,
* ofenzivní sílu soupeře (týmové OPS),
* oficiální lineupy, jakmile je tým zveřejní,
* seznam hráčů na IL.

Párování hráčů Sorare <-> MLB jede přes normalizované jméno + tým. Není to
neprůstřelné (Luis García, dvě verze jednoho jména), proto se každý nepárovaný
hráč hlásí a dá se ručně přemapovat v `manual_player_map`.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta
from functools import lru_cache

import requests

BASE = "https://statsapi.mlb.com/api/v1"

# Ruční přemapování pro případy, kdy automatika selže: sorare_slug -> mlb_id
manual_player_map: dict[str, int] = {}


def _get(path: str, **params) -> dict:
    resp = requests.get(f"{BASE}/{path.lstrip('/')}", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


# Přípony, které Sorare a MLB uvádějí nekonzistentně.
_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}


def normalize_name(name: str) -> str:
    text = unicodedata.normalize("NFKD", name)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z ]", " ", text.lower())
    parts = [p for p in text.split() if p and p not in _NAME_SUFFIXES]
    return " ".join(parts)


# ------------------------------------------------------------------ schedule


def schedule(start: date, end: date) -> list[dict]:
    """Zápasy v rozmezí, včetně probable pitcherů."""
    data = _get(
        "schedule",
        sportId=1,
        startDate=start.isoformat(),
        endDate=end.isoformat(),
        hydrate="probablePitcher,team,venue,linescore",
    )
    games: list[dict] = []
    for day in data.get("dates", []):
        for g in day.get("games", []):
            teams = g.get("teams", {})
            games.append(
                {
                    "game_pk": g.get("gamePk"),
                    "start": g.get("gameDate"),
                    "status": (g.get("status") or {}).get("abstractGameState"),
                    "venue": (g.get("venue") or {}).get("name"),
                    "home": _team_info(teams.get("home", {})),
                    "away": _team_info(teams.get("away", {})),
                }
            )
    return games


def _team_info(side: dict) -> dict:
    team = side.get("team") or {}
    probable = side.get("probablePitcher") or {}
    return {
        "id": team.get("id"),
        "name": team.get("name"),
        "abbrev": team.get("abbreviation"),
        "probable_pitcher_id": probable.get("id"),
        "probable_pitcher": probable.get("fullName"),
    }


def gameweek_range(reference: datetime | None = None) -> tuple[date, date]:
    """Sorare MLB má dva gameweeky: Po–Čt a Pá–Ne."""
    now = reference or datetime.now()
    weekday = now.weekday()  # 0 = pondělí
    if weekday <= 3:
        start = now.date() - timedelta(days=weekday)
        return start, start + timedelta(days=3)
    start = now.date() - timedelta(days=weekday - 4)
    return start, start + timedelta(days=2)


# ------------------------------------------------------------------ rosters


@lru_cache(maxsize=64)
def team_roster(team_id: int) -> tuple[dict, ...]:
    data = _get(f"teams/{team_id}/roster", rosterType="fullSeason")
    return tuple(
        {
            "id": (p.get("person") or {}).get("id"),
            "name": (p.get("person") or {}).get("fullName"),
            "position": (p.get("position") or {}).get("abbreviation"),
            "status": (p.get("status") or {}).get("description", ""),
        }
        for p in data.get("roster", [])
    )


def injured_player_ids(team_ids: list[int]) -> set[int]:
    out: set[int] = set()
    for tid in team_ids:
        for player in team_roster(tid):
            status = (player.get("status") or "").lower()
            if any(flag in status for flag in ("injured", "60-day", "restricted", "suspended")):
                out.add(player["id"])
    return out


def build_name_index(team_ids: list[int]) -> dict[str, dict]:
    """normalizované jméno -> záznam hráče. Kolize se řeší až v párování."""
    index: dict[str, dict] = {}
    for tid in team_ids:
        for player in team_roster(tid):
            if player.get("name"):
                index.setdefault(normalize_name(player["name"]), {**player, "team_id": tid})
    return index


def match_player(name_index: dict[str, dict], sorare_slug: str, name: str) -> dict | None:
    """Sorare hráč -> záznam z MLB rosteru. Jediné místo párování.

    Projekce i validátor musí párovat stejně, jinak ručně namapovaný hráč
    projde optimalizací a validátor ho pak hlásí jako nenapárovaného.
    """
    manual = manual_player_map.get(sorare_slug)
    if manual:
        for entry in name_index.values():
            if entry.get("id") == manual:
                return entry
    return name_index.get(normalize_name(name or ""))


# ------------------------------------------------------------------ stats


@lru_cache(maxsize=512)
def pitcher_stats(player_id: int, season: int) -> dict:
    """K%, ERA, WHIP pro nadhazovače."""
    try:
        data = _get(
            f"people/{player_id}/stats",
            stats="season",
            group="pitching",
            season=season,
        )
    except requests.HTTPError:
        return {}
    splits = ((data.get("stats") or [{}])[0].get("splits")) or []
    if not splits:
        return {}
    stat = splits[0].get("stat") or {}
    batters_faced = float(stat.get("battersFaced") or 0)
    strikeouts = float(stat.get("strikeOuts") or 0)
    return {
        "era": _as_float(stat.get("era")),
        "whip": _as_float(stat.get("whip")),
        "k_rate": (strikeouts / batters_faced) if batters_faced else None,
        "innings": _as_float(stat.get("inningsPitched")),
    }


@lru_cache(maxsize=64)
def team_offense(team_id: int, season: int) -> dict:
    try:
        data = _get(f"teams/{team_id}/stats", stats="season", group="hitting", season=season)
    except requests.HTTPError:
        return {}
    splits = ((data.get("stats") or [{}])[0].get("splits")) or []
    if not splits:
        return {}
    stat = splits[0].get("stat") or {}
    return {"ops": _as_float(stat.get("ops")), "runs": _as_float(stat.get("runs"))}


def confirmed_lineup(game_pk: int) -> dict[str, list[int]]:
    """Vrátí ID hráčů v oficiálním lineupu. Prázdné, dokud tým sestavu nezveřejní."""
    try:
        data = requests.get(
            f"https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live", timeout=30
        ).json()
    except (requests.RequestException, ValueError):
        return {}
    box = ((data.get("liveData") or {}).get("boxscore") or {}).get("teams") or {}
    out: dict[str, list[int]] = {}
    for side in ("home", "away"):
        battingOrder = (box.get(side) or {}).get("battingOrder") or []
        if battingOrder:
            out[side] = [int(pid) for pid in battingOrder]
    return out


# ------------------------------------------------------------------ ballpark

# Zjednodušené run park factory (1.00 = neutrál). Pro přesnější čísla je
# potřeba externí zdroj; tohle stačí jako slabý korekční člen.
PARK_FACTORS: dict[str, float] = {
    "Coors Field": 1.15,
    "Great American Ball Park": 1.08,
    "Fenway Park": 1.06,
    "Globe Life Field": 1.05,
    "Yankee Stadium": 1.04,
    "Chase Field": 1.03,
    "Wrigley Field": 1.02,
    "Citizens Bank Park": 1.02,
    "Dodger Stadium": 0.98,
    "Petco Park": 0.96,
    "Oracle Park": 0.94,
    "T-Mobile Park": 0.93,
    "loanDepot park": 0.92,
    "Tropicana Field": 0.92,
}


def park_factor(venue: str | None) -> float:
    return PARK_FACTORS.get(venue or "", 1.00)


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
