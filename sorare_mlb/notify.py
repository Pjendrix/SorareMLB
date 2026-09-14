"""Notifikace přes Discord nebo Telegram webhook.

Notifikace nikdy nesmí shodit job — každá chyba se jen zaloguje. Když ti
nepřijde zpráva, je to nepříjemné; když kvůli tomu spadne odeslání sestavy,
je to horší.
"""
from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger(__name__)


def _discord(webhook: str, text: str) -> None:
    requests.post(webhook, json={"content": text[:1900]}, timeout=15).raise_for_status()


def _telegram(token: str, chat_id: str, text: str) -> None:
    requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text[:4000], "parse_mode": "Markdown"},
        timeout=15,
    ).raise_for_status()


def notify(text: str) -> bool:
    """Pošle zprávu na nakonfigurovaný kanál. Vrací, zda se to povedlo."""
    discord = os.environ.get("DISCORD_WEBHOOK_URL")
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID")

    try:
        if discord:
            _discord(discord, text)
            return True
        if tg_token and tg_chat:
            _telegram(tg_token, tg_chat, text)
            return True
    except requests.RequestException as exc:
        log.warning("Notifikace selhala: %s", exc)
        return False

    log.info("Notifikace nenakonfigurovány, přeskakuji.")
    return False


def format_lineups(lineups, header: str) -> str:
    lines = [f"**{header}**"]
    for lineup in lineups:
        lines.append(
            f"\n__{lineup.tournament_name} #{lineup.index + 1}__ "
            f"— projekce {lineup.total_projected:.0f}"
        )
        for slot in lineup.slots:
            lines.append(f"`{slot.slot:<4}` {slot.player_name} ({slot.team or '—'}) "
                         f"· {slot.projected:.0f}")
    return "\n".join(lines)


def format_issues(issues) -> str:
    if not issues:
        return "Kontrola bez nálezů."
    lines = ["**Nálezy z kontroly sestav**"]
    for issue in issues:
        mark = "🔴" if issue.severity == "blocker" else "🟡"
        lines.append(f"{mark} {issue.lineup} / {issue.slot} — {issue.player}: {issue.message}")
        if issue.suggested_replacement:
            lines.append(f"    ↳ náhrada: {issue.suggested_replacement}")
    return "\n".join(lines)
