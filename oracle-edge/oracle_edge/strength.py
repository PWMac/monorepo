"""How strong is a signal? A simple, explainable rating shown next to every BUY / ADD / SELL / TRIM / REVIEW.

Buy side (BUY, STRONG BUY, ADD): 1 to 5 stars. Every buy already passed all gates (1 star), then one more star each for:
  * Buffett Rank score >= 65
  * Buffett Rank score >= 75
  * Quality >= 80
  * a strong-buy price (margin of safety >= the buy gate + 15%)
Sell side: SELL = act (high), TRIM = medium, REVIEW = high if it can become a SELL, else low (watch only), SWAP = upgrade.
"""
from __future__ import annotations

import html

LABELS = {5: "Very strong", 4: "Strong", 3: "Good", 2: "Fair", 1: "Marginal"}
BUY_TYPES = ("BUY", "STRONG_BUY", "ADD")


def buy_stars(rank_score, quality, mos, mos_gate: float = 0.25, extra: float = 0.15) -> int | None:
    if rank_score is None:
        return None
    s = 1 + (rank_score >= 65) + (rank_score >= 75) + ((quality or 0) >= 80) + ((mos or 0) >= mos_gate + extra)
    return int(min(5, s))


def sell_level(sig_type: str, reason: str = "") -> tuple[str, str]:
    """(label, colour) for sell-side signals."""
    if sig_type == "SELL":
        return "Act: sell", "#a33a2b"
    if sig_type == "SWAP":
        return "Upgrade", "#2c5d8f"
    if sig_type == "TRIM":
        return "Medium: trim", "#7a4f9e"
    if sig_type == "REVIEW":
        if "becomes SELL" in (reason or ""):
            return "High: may become a sell", "#a24d00"
        return "Low: watch only", "#9a5b00"
    return "", "#444"


def stars_html(n: int | None, small: bool = False) -> str:
    if not n:
        return ""
    size = "13px" if small else "15px"
    return (f'<span title="{LABELS[n]} ({n}/5)" style="color:#c08a2e;font-size:{size};letter-spacing:1px;'
            f'white-space:nowrap">{"★" * n}<span style="color:#d9d6cc">{"★" * (5 - n)}</span></span>')


def strength_html(sig_type: str, rank_score=None, quality=None, mos=None, reason: str = "", mos_gate: float = 0.25,
                  small: bool = False) -> str:
    """Stars + label for buys, a coloured severity tag for sells."""
    if sig_type in BUY_TYPES:
        n = buy_stars(rank_score, quality, mos, mos_gate)
        if not n:
            return ""
        return f'{stars_html(n, small)} <span style="font-size:11.5px;color:#5d6763">{LABELS[n]}</span>'
    label, col = sell_level(sig_type, reason)
    if not label:
        return ""
    return (f'<span style="font-size:11.5px;font-weight:700;color:{col};border:1.5px solid {col};border-radius:999px;'
            f'padding:0 7px;white-space:nowrap">{html.escape(label)}</span>')


def metrics_text(rank=None, rank_score=None, quality=None, value=None, mos=None, er=None) -> str:
    parts = []
    if rank:
        parts.append(f"Rank #{rank}")
    if rank_score is not None:
        parts.append(f"score {rank_score:.0f}")
    if quality is not None:
        parts.append(f"Quality {quality:.0f}")
    if value is not None:
        parts.append(f"Value {value:.0f}")
    if mos is not None:
        parts.append(f"MOS {mos:.0%}")
    if er is not None:
        parts.append(f"exp. return {er:.1%}")
    return " · ".join(parts)
