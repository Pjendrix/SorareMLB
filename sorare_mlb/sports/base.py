"""Společné rozhraní sportu."""
from __future__ import annotations

from datetime import date, datetime

from ..client import SorareClient, card_from_dict
from ..models import Card, Config


class NotSupported(NotImplementedError):
    """Funkce pro tenhle sport zatím není hotová."""


class SportAdapter:
    key: str = ""            # interní klíč (url, config)
    sport: str = ""          # hodnota enumu Sport v Sorare API
    label: str = ""
    lineups_supported: bool = False

    # ------------------------------------------------------------ čtení (vše)

    def config(self, config: Config) -> dict:
        return (config.get("sports") or {}).get(self.key) or {}

    def fetch_cards(self, client: SorareClient, config: Config) -> tuple[list[Card], bool]:
        cfg = self.config(config)
        rarities = cfg.get("rarities") or config.get("rarities", ["limited", "rare"])
        nodes, truncated = client.fetch_sport_cards(
            self.sport, rarities, max_pages=int(cfg.get("max_card_pages", 15))
        )
        return [card_from_dict(n) for n in nodes], truncated

    def position_label(self, position: str) -> str:
        return position.replace(f"{self.sport}_", "").replace("_", " ").title()

    def summarize(self, cards: list[Card], config: Config, today: date | None = None) -> dict:
        """Souhrn sbírky pro přehled. Čistá funkce — testuje se bez sítě."""
        today = today or date.today()
        idle_days = int(self.config(config).get("idle_after_days", 14))

        by_rarity: dict[str, int] = {}
        by_position: dict[str, int] = {}
        rows = []
        for card in cards:
            by_rarity[card.rarity] = by_rarity.get(card.rarity, 0) + 1
            for pos in card.positions[:1]:
                label = self.position_label(pos)
                by_position[label] = by_position.get(label, 0) + 1

            last = _parse_date(card.last_game)
            days_since = (today - last).days if last else None
            rows.append(
                {
                    "slug": card.slug,
                    "player": card.player.name,
                    "team": card.player.team_name,
                    "rarity": card.rarity,
                    "season": card.season,
                    "position": self.position_label(card.positions[0]) if card.positions else "—",
                    "l5": _round(card.avg_last(5)),
                    "l15": _round(card.avg_last(15)),
                    "trend": _trend(card.recent_scores),
                    "last_game": last.isoformat() if last else None,
                    "days_since_game": days_since,
                    "scores": list(reversed(card.recent_scores[:10])),
                }
            )

        l15s = [r["l15"] for r in rows if r["l15"] is not None]
        idle = [r for r in rows if r["days_since_game"] is None or r["days_since_game"] > idle_days]
        active = [r for r in rows if r["l15"] is not None and r not in idle]

        return {
            "count": len(rows),
            "by_rarity": dict(sorted(by_rarity.items(), key=lambda kv: -kv[1])),
            "by_position": dict(sorted(by_position.items(), key=lambda kv: -kv[1])),
            "avg_l15": _round(sum(l15s) / len(l15s)) if l15s else None,
            "in_form": sorted(active, key=lambda r: -(r["l5"] or 0))[:8],
            "cooling": sorted(
                [r for r in active if r["trend"] is not None and r["trend"] < 0], key=lambda r: r["trend"]
            )[:5],
            "idle": sorted(idle, key=lambda r: -(r["days_since_game"] or 10**4))[:12],
            "idle_count": len(idle),
            "idle_after_days": idle_days,
            "cards": sorted(rows, key=lambda r: -(r["l15"] or -1)),
        }

    # ------------------------------------------------------------ sestavy

    def build_lineups(self, *args, **kwargs):
        raise NotSupported(f"Skládání sestav pro {self.label} zatím není hotové.")

    def validate_lineups(self, *args, **kwargs):
        raise NotSupported(f"Kontrola sestav pro {self.label} zatím není hotová.")

    def submit_lineups(self, *args, **kwargs):
        raise NotSupported(f"Odesílání sestav pro {self.label} zatím není hotové.")


def _parse_date(value) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None


def _round(value) -> float | None:
    return None if value is None else round(float(value), 1)


def _trend(scores: list[float]) -> float | None:
    """L5 minus L15. Kladné = hráč se zlepšuje."""
    if len(scores) < 6:
        return None
    l5 = sum(scores[:5]) / 5
    l15 = sum(scores[:15]) / len(scores[:15])
    return round(l5 - l15, 1)


__all__ = ["SportAdapter", "NotSupported"]
