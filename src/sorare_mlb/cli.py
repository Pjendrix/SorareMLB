"""Příkazová řádka."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from . import mlb
from .auth import ensure_token
from .client import SorareClient, SorareError
from .models import Config, Lineup, LineupSlot, Tournament
from .optimizer import LineupOptimizer, OptimizationError
from .projections import ProjectionEngine
from .validator import LineupValidator

console = Console()
OUT_DIR = Path("out")


# --------------------------------------------------------------------- shared


def _context(args) -> tuple[Config, SorareClient]:
    config = Config.load(args.config)
    client = SorareClient(config, auth_method=args.auth)
    return config, client


def _gather(config: Config, client: SorareClient, use_cache: bool = True):
    """Stáhne vše potřebné: karty, skóre, MLB kontext, projekce."""
    console.print("[dim]Stahuji portfolio…[/dim]")
    cards = client.fetch_cards(use_cache=use_cache)
    console.print(f"  {len(cards)} karet")

    player_slugs = sorted({c.player.slug for c in cards if c.player.slug})
    console.print("[dim]Stahuji Sorare skóre…[/dim]")
    scores = client.fetch_scores(player_slugs)

    start, end = mlb.gameweek_range()
    console.print(f"[dim]MLB rozpis {start} – {end}…[/dim]")
    games = mlb.schedule(start, end)
    console.print(f"  {len(games)} zápasů")

    team_ids = sorted(
        {g[side]["id"] for g in games for side in ("home", "away") if g[side].get("id")}
    )
    name_index = mlb.build_name_index(team_ids)
    injured = mlb.injured_player_ids(team_ids)

    engine = ProjectionEngine(config, games, name_index, injured)
    projections = {c.slug: engine.project(c, scores.get(c.player.slug, [])) for c in cards}

    unmatched = [
        c.player.name
        for c in cards
        if projections[c.slug].reason_unplayable == "hráč nenalezen v MLB rosteru"
    ]
    if unmatched:
        console.print(
            f"[yellow]Nenapárováno {len(unmatched)} hráčů:[/yellow] "
            + ", ".join(sorted(set(unmatched))[:10])
            + ("…" if len(set(unmatched)) > 10 else "")
        )
        console.print("[dim]Doplň je do mlb.manual_player_map, pokud mají hrát.[/dim]")

    return cards, projections, games, name_index, injured


def _tournaments(config: Config, client: SorareClient) -> list[Tournament]:
    fixture = client.fetch_open_fixture()
    if not fixture:
        raise SorareError("Nenašel jsem otevřený fixture. Možná je mezi gameweeky.")
    console.print(f"[dim]Fixture: {fixture.get('displayName') or fixture.get('slug')}[/dim]")

    available = client.fetch_competitions(fixture["slug"])
    result: list[Tournament] = []
    for spec in config.get("tournaments", []):
        needle = str(spec.get("slug_contains", "")).lower()
        match = next((c for c in available if needle in str(c.get("slug", "")).lower()), None)
        if not match:
            console.print(f"[yellow]Turnaj '{spec['name']}' nenalezen ve fixture — přeskakuji.[/yellow]")
            continue
        already = int(match.get("lineupsCount") or 0)
        cap = int(match.get("maxLineups") or spec.get("max_lineups", 1))
        remaining = max(0, min(int(spec.get("max_lineups", 1)), cap - already))
        if remaining == 0:
            console.print(f"[dim]{spec['name']}: sestavy už odeslané, přeskakuji.[/dim]")
            continue
        result.append(
            Tournament(
                slug=match["slug"],
                name=spec["name"],
                weight=float(spec.get("weight", 1.0)),
                risk_mode=spec.get("risk_mode", "upside"),
                require_confirmed_lineup=bool(spec.get("require_confirmed_lineup", False)),
                max_lineups=remaining,
            )
        )
    return result


# --------------------------------------------------------------------- commands


def cmd_login(args) -> int:
    token = ensure_token(args.auth, force=True)
    console.print(f"[green]Přihlášen[/green] ({token.method}), token platí do "
                  f"{datetime.fromtimestamp(token.expires_at):%Y-%m-%d %H:%M}")
    return 0


def cmd_probe(args) -> int:
    """Ověří, že dotazy v queries.py sedí na aktuální schéma."""
    config, client = _context(args)
    schema = client.introspect_root()
    q_fields = {f["name"] for f in (schema.get("queryType") or {}).get("fields", [])}
    m_fields = {f["name"] for f in (schema.get("mutationType") or {}).get("fields", [])}

    table = Table(title="Kontrola schématu")
    table.add_column("Co")
    table.add_column("Stav")
    for name in ("currentUser", "baseball", "baseballPlayers"):
        table.add_row(f"query.{name}", "[green]OK[/green]" if name in q_fields else "[red]CHYBÍ[/red]")
    for name in ("createBaseballLineup", "submitLineup", "signIn"):
        table.add_row(f"mutation.{name}", "[green]OK[/green]" if name in m_fields else "[red]CHYBÍ[/red]")
    console.print(table)

    for type_name in ("createBaseballLineupInput", "submitLineupInput"):
        info = client.introspect_type(type_name)
        if info:
            fields = ", ".join(f["name"] for f in (info.get("inputFields") or []))
            console.print(f"[cyan]{type_name}[/cyan]: {fields}")

    console.print(
        "\n[dim]Cokoli označeného CHYBÍ oprav v src/sorare_mlb/queries.py "
        "podle výpisu výše.[/dim]"
    )
    return 0


def cmd_cards(args) -> int:
    config, client = _context(args)
    cards = client.fetch_cards(use_cache=not args.refresh)
    table = Table(title=f"Portfolio ({len(cards)} karet)")
    for col in ("Hráč", "Tým", "Pozice", "Rarita", "Sezóna"):
        table.add_column(col)
    for card in sorted(cards, key=lambda c: c.player.name):
        table.add_row(
            card.player.name,
            card.player.team_name or "—",
            ", ".join(card.positions),
            card.rarity,
            str(card.season or "—"),
        )
    console.print(table)
    return 0


def cmd_build(args) -> int:
    config, client = _context(args)
    cards, projections, games, name_index, injured = _gather(config, client, not args.refresh)
    tournaments = _tournaments(config, client)
    if not tournaments:
        console.print("[red]Není co obsazovat.[/red]")
        return 1

    playable = sum(1 for p in projections.values() if p.playable)
    console.print(f"[dim]Použitelných karet: {playable} / {len(projections)}[/dim]")

    optimizer = LineupOptimizer(config, cards, projections)
    try:
        lineups = optimizer.solve(tournaments)
    except OptimizationError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    for lineup in lineups:
        _print_lineup(lineup, projections, explain=args.explain)

    OUT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    path = OUT_DIR / f"lineups-{stamp}.json"
    path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "lineups": [lu.to_dict() for lu in lineups],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    console.print(f"\n[green]Uloženo:[/green] {path}")
    console.print(f"[dim]Odeslání: python -m sorare_mlb submit {path}[/dim]")
    return 0


def cmd_check(args) -> int:
    config, client = _context(args)
    lineups = _load_lineups(args.file)
    cards, projections, games, name_index, injured = _gather(config, client)

    validator = LineupValidator(config, cards, projections, games, name_index, injured)
    issues = validator.validate(lineups)

    if not issues:
        console.print("[green]Vše v pořádku, žádné problémy.[/green]")
        return 0

    table = Table(title="Nálezy")
    for col in ("Turnaj", "Slot", "Hráč", "Závažnost", "Problém", "Návrh náhrady"):
        table.add_column(col)
    for issue in issues:
        color = "red" if issue.severity == "blocker" else "yellow"
        table.add_row(
            issue.lineup, issue.slot, issue.player,
            f"[{color}]{issue.severity}[/{color}]",
            issue.message, issue.suggested_replacement or "—",
        )
    console.print(table)
    blockers = sum(1 for i in issues if i.severity == "blocker")
    console.print(
        f"\n[dim]Náhrady se neprovádějí automaticky. Uprav {args.file} a odešli znovu.[/dim]"
    )
    return 1 if blockers else 0


def cmd_submit(args) -> int:
    config, client = _context(args)
    lineups = _load_lineups(args.file)

    console.print("[bold]Následující sestavy se odešlou na Sorare:[/bold]\n")
    for lineup in lineups:
        _print_lineup(lineup, {}, explain=False)

    if not args.yes:
        answer = console.input("\n[bold yellow]Odeslat? napiš 'ano':[/bold yellow] ").strip().lower()
        if answer not in ("ano", "yes", "y"):
            console.print("Zrušeno, nic se neodeslalo.")
            return 0

    failures = 0
    for lineup in lineups:
        try:
            result = client.submit_lineup(lineup.tournament_slug, lineup.card_slugs)
            lineup_id = (result.get("lineup") or {}).get("id", "?")
            console.print(f"[green]OK[/green] {lineup.tournament_name} → id {lineup_id}")
        except SorareError as exc:
            failures += 1
            console.print(f"[red]SELHALO[/red] {lineup.tournament_name}: {exc}")
    return 1 if failures else 0


# --------------------------------------------------------------------- helpers


def _load_lineups(path: str) -> list[Lineup]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out: list[Lineup] = []
    for raw in data.get("lineups", []):
        out.append(
            Lineup(
                tournament_slug=raw["tournament_slug"],
                tournament_name=raw["tournament_name"],
                index=raw.get("index", 0),
                slots=[
                    LineupSlot(
                        slot=s["slot"],
                        card_slug=s["card_slug"],
                        player_name=s["player"],
                        team=s.get("team"),
                        projected=float(s.get("projected", 0)),
                    )
                    for s in raw["slots"]
                ],
            )
        )
    return out


def _print_lineup(lineup: Lineup, projections: dict, explain: bool) -> None:
    title = f"{lineup.tournament_name} #{lineup.index + 1}  —  projekce {lineup.total_projected:.1f}"
    table = Table(title=title, title_justify="left")
    for col in ("Slot", "Hráč", "Tým", "Proj."):
        table.add_column(col)
    if explain:
        table.add_column("Floor/Ceil")
        table.add_column("Poznámky")

    for slot in lineup.slots:
        row = [slot.slot, slot.player_name, slot.team or "—", f"{slot.projected:.1f}"]
        if explain:
            proj = projections.get(slot.card_slug)
            row.append(f"{proj.floor:.0f} / {proj.ceiling:.0f}" if proj else "—")
            row.append("; ".join(proj.notes) if proj and proj.notes else "—")
        table.add_row(*row)
    console.print(table)


# --------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="sorare_mlb", description="Sorare MLB Lineup Builder")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--auth", choices=["password", "oauth"], default="password")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="přihlásí se a uloží token").set_defaults(func=cmd_login)
    sub.add_parser("probe", help="ověří GraphQL schéma").set_defaults(func=cmd_probe)

    p_cards = sub.add_parser("cards", help="vypíše portfolio")
    p_cards.add_argument("--refresh", action="store_true", help="ignoruj cache")
    p_cards.set_defaults(func=cmd_cards)

    p_build = sub.add_parser("build", help="navrhne sestavy")
    p_build.add_argument("--refresh", action="store_true")
    p_build.add_argument("--explain", action="store_true", help="rozpad projekcí")
    p_build.set_defaults(func=cmd_build)

    p_check = sub.add_parser("check", help="kontrola sestav před deadlinem")
    p_check.add_argument("file")
    p_check.set_defaults(func=cmd_check)

    p_submit = sub.add_parser("submit", help="odešle sestavy po potvrzení")
    p_submit.add_argument("file")
    p_submit.add_argument("--yes", action="store_true", help="přeskoč potvrzení")
    p_submit.set_defaults(func=cmd_submit)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (SorareError, RuntimeError, FileNotFoundError) as exc:
        console.print(f"[red]Chyba:[/red] {exc}")
        return 1
    except KeyboardInterrupt:
        console.print("\nPřerušeno.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
