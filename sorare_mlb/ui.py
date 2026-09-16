"""Stránky aplikace. Každá se renderuje jednou při startu funkce."""
from .sports.football import FootballAdapter
from .web import (
    dashboard, football_lineups, history, ledger_page, mlb_lineups, rewards_page, settings, sport,
)

PAGES = {
    "/": dashboard.render(),
    "/vyhry": rewards_page.render(),
    "/bilance": ledger_page.render(),
    "/mlb": sport.render(
        "mlb", "mlb", "MLB",
        "Sbírka baseballových karet, forma hráčů a otevřené turnaje.",
        actions='<a class="btn" href="/mlb/sestavy">Sestavy MLB</a>',
    ),
    "/mlb/sestavy": mlb_lineups.render(),
    "/mlb/historie": history.render("mlb", "mlb-history", "MLB", show_runs=True),
    "/fotbal": sport.render(
        "football", "football", "Fotbal",
        "Sbírka fotbalových karet, forma hráčů a otevřené turnaje.",
        actions='<a class="btn" href="/fotbal/sestavy">Sestavy fotbal</a>',
    ),
    "/fotbal/sestavy": football_lineups.render(FootballAdapter.PLANNED_SLOTS),
    "/fotbal/historie": history.render("football", "football-history", "fotbal", show_runs=False),
    "/nastaveni": settings.render(),
}

# Zpětná kompatibilita pro starý import.
PAGE = PAGES["/mlb/sestavy"]
