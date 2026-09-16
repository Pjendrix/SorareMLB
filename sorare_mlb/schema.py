"""Práce s veřejným GraphQL schématem Sorare (SDL).

Introspekce je pro API klíče zakázaná a část polí (odměny, Essence, trezor)
se v dokumentaci popisuje jen útržkovitě. Místo hádání si aplikace stáhne
veřejné SDL a dotazy poskládá jen z polí, která ve schématu opravdu jsou.

Samotné SDL má přes megabajt (víc, než unese hodnota v Upstash free), takže
se do Redisu ukládají jen hotová rozhodnutí (`features`), ne schéma.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import requests

SDL_URL = "https://api.sorare.com/graphql/schema"


@dataclass
class FieldDef:
    name: str
    type: str                     # včetně [] a !, např. "[AnyCardInterface!]!"
    args: list[str] = field(default_factory=list)
    arg_types: dict[str, str] = field(default_factory=dict)

    @property
    def base(self) -> str:
        return re.sub(r"[\[\]!\s]", "", self.type)


@dataclass
class TypeDef:
    kind: str                     # type | interface | input | union | enum
    name: str
    fields: dict[str, FieldDef] = field(default_factory=dict)
    implements: list[str] = field(default_factory=list)
    members: list[str] = field(default_factory=list)   # union


class Schema:
    def __init__(self, sdl: str):
        self.types: dict[str, TypeDef] = {}
        self._parse(_strip_descriptions(sdl))

    # ------------------------------------------------------------ dotazy

    def has(self, type_name: str, field_name: str) -> bool:
        t = self.types.get(type_name)
        return bool(t and field_name in t.fields)

    def field(self, type_name: str, field_name: str) -> FieldDef | None:
        t = self.types.get(type_name)
        return t.fields.get(field_name) if t else None

    def pick(self, type_name: str, *candidates: str) -> str | None:
        """První kandidát, který na typu existuje."""
        return next((c for c in candidates if self.has(type_name, c)), None)

    def find(self, type_name: str, pattern: str, base: str | None = None) -> list[str]:
        t = self.types.get(type_name)
        if not t:
            return []
        rx = re.compile(pattern, re.I)
        return [
            f.name for f in t.fields.values()
            if rx.search(f.name) and (base is None or f.base == base)
        ]

    def possible_types(self, name: str) -> list[str]:
        """Konkrétní typy, které lze použít ve fragmentu na union/interface."""
        t = self.types.get(name)
        if not t:
            return []
        if t.kind == "union":
            return list(t.members)
        if t.kind == "interface":
            return [x.name for x in self.types.values() if name in x.implements]
        return [name]

    def node_type(self, type_name: str, field_name: str) -> str | None:
        """Typ položek za connection (`cards` → `nodes` → typ karty)."""
        f = self.field(type_name, field_name)
        if not f:
            return None
        nodes = self.field(f.base, "nodes")
        return nodes.base if nodes else f.base

    # ------------------------------------------------------------ parser

    def _parse(self, sdl: str) -> None:
        head = re.compile(
            r"^(type|interface|input|union|enum)\s+(\w+)([^{=\n]*)(\{|=)?", re.M
        )
        for m in head.finditer(sdl):
            kind, name, rest, opener = m.group(1), m.group(2), m.group(3), m.group(4)
            t = TypeDef(kind=kind, name=name)
            impl = re.search(r"implements\s+([\w\s&,]+)", rest or "")
            if impl:
                t.implements = [x for x in re.split(r"[\s&,]+", impl.group(1)) if x]

            if opener == "=":
                line_end = sdl.find("\n", m.end())
                body = sdl[m.end(): line_end if line_end > 0 else None]
                # union může pokračovat na dalších řádcích začínajících "|"
                pos = line_end
                while pos > 0:
                    nxt = sdl.find("\n", pos + 1)
                    line = sdl[pos + 1: nxt if nxt > 0 else None]
                    if not line.strip().startswith("|"):
                        break
                    body += line
                    pos = nxt
                t.members = [x for x in re.split(r"[\s|]+", body) if x]
            elif opener == "{":
                end = _matching_brace(sdl, m.end() - 1)
                if kind in ("type", "interface", "input"):
                    t.fields = _parse_fields(sdl[m.end(): end])
            self.types[name] = t


def _strip_descriptions(sdl: str) -> str:
    sdl = re.sub(r'"""[\s\S]*?"""', "", sdl)
    sdl = re.sub(r'^\s*"(?:[^"\\\n]|\\.)*"\s*$', "", sdl, flags=re.M)
    return re.sub(r"#[^\n]*", "", sdl)


def _matching_brace(text: str, start: int) -> int:
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return len(text)


def _parse_fields(body: str) -> dict[str, FieldDef]:
    # Argumenty (i víceřádkové) nahradíme značkou s indexem a jejich názvy
    # si uložíme zvlášť. Direktivy (@deprecated(...)) pak zmizí celé.
    args: list[list[tuple[str, str]]] = []
    flat: list[str] = []
    depth, buf = 0, ""
    for ch in body:
        if ch == "(":
            depth += 1
            if depth == 1:
                buf = ""
                continue
        elif ch == ")":
            depth -= 1
            if depth == 0:
                flat.append(f"\x00{len(args)}\x01")
                args.append(re.findall(r"(\w+)\s*:\s*([\w\[\]!]+)", buf))
                continue
        if depth:
            buf += ch
        else:
            flat.append(ch)
    text = re.sub(r"@\w+(\x00\d+\x01)?", " ", "".join(flat))
    text = re.sub(r"=\s*[^\s]+", " ", text)          # výchozí hodnoty u inputů
    out: dict[str, FieldDef] = {}
    for m in re.finditer(r"(\w+)\s*(?:\x00(\d+)\x01)?\s*:\s*([\w\[\]!]+)", text):
        idx = m.group(2)
        pairs = args[int(idx)] if idx else []
        out[m.group(1)] = FieldDef(
            m.group(1), m.group(3), [n for n, _ in pairs], dict(pairs)
        )
    return out


# ------------------------------------------------------------------ načtení

_cached: Schema | None = None


def load_schema(sdl: str | None = None) -> Schema:
    global _cached
    if sdl is not None:
        return Schema(sdl)
    if _cached is None:
        resp = requests.get(SDL_URL, timeout=60)
        resp.raise_for_status()
        _cached = Schema(resp.text)
    return _cached
