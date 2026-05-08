"""Telegram bot push for the daily-tick summary.

Free, simple, reliable. No third-party libs needed — direct sendMessage call.

Setup:
  1. Create a bot via BotFather (@BotFather on Telegram), get TELEGRAM_BOT_TOKEN
  2. Send any message to your bot, then visit https://api.telegram.org/bot<TOKEN>/getUpdates
     to find your TELEGRAM_CHAT_ID
  3. Put both in .env. send_telegram() becomes a no-op if either is missing.
"""
from __future__ import annotations

import requests
from loguru import logger

from stockagent.config import settings


_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def telegram_configured() -> bool:
    return bool(settings.telegram_bot_token and settings.telegram_chat_id)


def send_telegram(message: str, *, parse_mode: str = "HTML", disable_web_preview: bool = True) -> bool:
    """Push `message` to the configured chat. Returns True on success.

    Telegram has a 4096-char hard limit; we truncate at 4000 with an ellipsis.
    """
    if not telegram_configured():
        logger.debug("telegram not configured; skipping send")
        return False
    if len(message) > 4000:
        message = message[:3990] + "\n…(truncated)"
    url = _API_URL.format(token=settings.telegram_bot_token)
    try:
        r = requests.post(
            url,
            json={
                "chat_id": settings.telegram_chat_id,
                "text": message,
                "parse_mode": parse_mode,
                "disable_web_page_preview": disable_web_preview,
            },
            timeout=10,
        )
        if r.status_code == 200:
            return True
        logger.warning(f"telegram send: HTTP {r.status_code} {r.text[:200]}")
    except requests.RequestException as e:
        logger.warning(f"telegram send: {e}")
    return False


def format_daily_summary(*, as_of, nav: float, day_pnl: float, open_count: int,
                          fills: int, exits: dict, picks: list) -> str:
    """Format the daily-tick summary for Telegram. HTML markup."""
    starting = settings.capital_inr
    ret_pct = (nav - starting) / starting * 100
    direction = "🟢" if day_pnl >= 0 else "🔴"
    lines = [
        f"<b>📈 stockagent daily — {as_of}</b>",
        f"NAV ₹{nav:,.0f}  ({ret_pct:+.2f}% from start)",
        f"{direction} day P&L ₹{day_pnl:+,.0f}",
        f"open: {open_count}  fills: {fills}  exits: stop={exits.get('stop',0)} sig={exits.get('signal',0)} time={exits.get('time',0)}",
        "",
    ]
    if picks:
        lines.append(f"<b>Tomorrow's watchlist ({len(picks)} picks)</b>")
        for i, p in enumerate(picks, 1):
            rr = (p.target - p.entry) / (p.entry - p.stop) if p.target and p.entry > p.stop else 0
            lines.append(
                f"{i}. <b>{p.symbol}</b>  ({getattr(p, 'sector', '-')})\n"
                f"   ₹{p.entry:.2f} → stop ₹{p.stop:.2f} → tgt ₹{p.target:.2f}  R:R 1:{rr:.1f}\n"
                f"   qty {p.qty}  ₹{p.position_size_inr:,.0f}  conv {p.conviction:.2f}"
            )
    else:
        lines.append("<i>No qualifying signals today.</i>")
    return "\n".join(lines)
