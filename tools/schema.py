"""Stáhne veřejné GraphQL schéma Sorare a vypíše definici typu.

Introspekce je pro API klíče zakázaná, ale SDL je veřejně ke stažení.
Použití (lokálně):

    python tools/schema.py So5Lineup
    python tools/schema.py CurrentUser --grep reward
    python tools/schema.py --search so5Rankings

Schéma se uloží do tools/schema.graphql (je v .gitignore) a znovu se
stáhne jen s --refresh.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import requests

URL = "https://api.sorare.com/graphql/schema"
CACHE = Path(__file__).with_name("schema.graphql")


def load(refresh: bool) -> str:
    if refresh or not CACHE.exists():
        resp = requests.get(URL, timeout=60)
        resp.raise_for_status()
        CACHE.write_text(resp.text, encoding="utf-8")
    return CACHE.read_text(encoding="utf-8")


def block(sdl: str, name: str) -> str | None:
    match = re.search(
        r"^(?:type|input|interface|enum|union)\s+" + re.escape(name) + r"\b[^\n]*", sdl, re.M
    )
    if not match:
        return None
    if "{" not in match.group(0):
        return match.group(0)
    depth, i = 0, sdl.index("{", match.start())
    while i < len(sdl):
        depth += {"{": 1, "}": -1}.get(sdl[i], 0)
        if depth == 0:
            break
        i += 1
    return sdl[match.start(): i + 1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("type", nargs="?", help="název typu, např. So5Lineup")
    ap.add_argument("--grep", help="jen řádky obsahující text")
    ap.add_argument("--search", help="najdi typy, které obsahují pole/text")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    sdl = load(args.refresh)

    if args.search:
        needle = args.search.lower()
        for m in re.finditer(r"^(?:type|interface|input)\s+(\w+)", sdl, re.M):
            text = block(sdl, m.group(1)) or ""
            hits = [ln.strip() for ln in text.splitlines() if needle in ln.lower()]
            if hits:
                print(f"{m.group(1)}: " + " | ".join(hits[:3]))
        return 0

    if not args.type:
        ap.print_help()
        return 1
    text = block(sdl, args.type)
    if text is None:
        print(f"Typ {args.type} neexistuje.")
        return 1
    lines = text.splitlines()
    if args.grep:
        lines = [ln for ln in lines if args.grep.lower() in ln.lower()]
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
