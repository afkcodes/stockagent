"""Telegram bot push for the daily-tick summary.

Free, simple, reliable. Direct REST calls, no extra lib.

Setup:
  1. Create a bot via BotFather (@BotFather on Telegram), get TELEGRAM_BOT_TOKEN
  2. Use `stockagent telegram-init` to discover your chat_id
  3. Put both in .env. send functions become no-ops if either is missing.

Two delivery formats:
  - send_telegram(text)              — HTML text message
  - send_telegram_photo(png_bytes)   — image (preferred for daily summary)

`send_daily_summary()` is the high-level wrapper used by daily-tick.
It renders a clean PNG card via PIL and sends as a photo, with an
automatic text fallback if rendering or upload fails.
"""
from __future__ import annotations

import io
from pathlib import Path

import requests
from loguru import logger

from stockagent.config import settings


_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
_API_PHOTO_URL = "https://api.telegram.org/bot{token}/sendPhoto"


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
    """Format the daily-tick summary for Telegram. HTML markup. Used as text fallback."""
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


# ────────────────────────────────────────────────────────────────────────────
# Image rendering — PIL-based, simple monospace card on white
# ────────────────────────────────────────────────────────────────────────────

def _find_font(size: int, bold: bool = False, mono: bool = False):
    """Find a usable TrueType font on the system. Falls back to PIL default."""
    from PIL import ImageFont

    candidates = []
    if mono:
        candidates += [
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf" if bold else None,
            "/System/Library/Fonts/Menlo.ttc",
            "/Library/Fonts/Courier New.ttf",
        ]
    elif bold:
        candidates += [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
        ]
    else:
        candidates += [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
        ]
    for path in [c for c in candidates if c]:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def render_daily_summary_image(*, as_of, nav: float, day_pnl: float, open_count: int,
                                fills: int, exits: dict, picks: list) -> bytes:
    """Render the daily summary as a clean PNG card. Returns image bytes.
    Plain monospace text on white; no fancy styling. Easy to read at a glance."""
    from PIL import Image, ImageDraw

    starting = settings.capital_inr
    ret_pct = (nav - starting) / starting * 100
    pnl_color = (16, 124, 16) if day_pnl >= 0 else (192, 32, 32)
    grey = (90, 90, 90)
    light_grey = (200, 200, 200)

    title_font = _find_font(20, bold=True)
    section_font = _find_font(15, bold=True)
    body_font = _find_font(13, mono=True)
    body_bold = _find_font(13, mono=True, bold=True)

    width = 760
    pad = 24
    line_h = 22

    # Build a list of (text, font, color) — None for blank line spacers.
    # Note: DejaVuSansMono lacks the ₹ glyph and renders it as a box. Use "Rs"
    # prefix to keep the card readable in any font available on Linux servers.
    rows: list[tuple[str | None, object, tuple]] = []
    rows.append((f"stockagent daily  -  {as_of}", title_font, (0, 0, 0)))
    rows.append((None, body_font, (0, 0, 0)))  # spacer
    rows.append((f"NAV       Rs {nav:>13,.0f}    ({ret_pct:+.2f}% from start)", body_font, (0, 0, 0)))
    rows.append((f"Day P&L   Rs {day_pnl:>+13,.0f}", body_bold, pnl_color))
    rows.append((f"Open: {open_count}   Fills: {fills}   Exits  stop:{exits.get('stop',0)}  signal:{exits.get('signal',0)}  time:{exits.get('time',0)}",
                 body_font, grey))
    rows.append((None, body_font, (0, 0, 0)))

    if picks:
        rows.append((f"Tomorrow's watchlist  ({len(picks)} picks)", section_font, (0, 0, 0)))
        rows.append(("-" * 78, body_font, light_grey))
        for i, p in enumerate(picks, 1):
            sector = (getattr(p, "sector", "-") or "-")[:14]
            rr = (p.target - p.entry) / (p.entry - p.stop) if p.target and p.entry > p.stop else 0
            rows.append((f"{i}. {p.symbol:<14s} {sector:<14s}  conviction {p.conviction:.2f}",
                         body_bold, (0, 0, 0)))
            rows.append((f"   entry Rs {p.entry:>9,.2f}   stop Rs {p.stop:>9,.2f}   tgt Rs {p.target:>9,.2f}   R:R 1:{rr:.1f}",
                         body_font, grey))
            rows.append((f"   qty {p.qty:>4}   alloc Rs {p.position_size_inr:>9,.0f}",
                         body_font, grey))
            if i < len(picks):
                rows.append((None, body_font, (0, 0, 0)))
    else:
        rows.append(("No qualifying signals today.", body_font, grey))

    # Compute height: full line for text rows, half for spacers
    height = pad + sum(line_h if r[0] is not None else line_h // 2 for r in rows) + pad

    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)

    y = pad
    for text, font, color in rows:
        if text is not None:
            draw.text((pad, y), text, fill=color, font=font)
            y += line_h
        else:
            y += line_h // 2

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def send_telegram_photo(image_bytes: bytes, caption: str = "") -> bool:
    """Send a PNG (or any image) via Telegram sendPhoto. Caption max 1024 chars."""
    if not telegram_configured():
        logger.debug("telegram not configured; skipping photo send")
        return False
    url = _API_PHOTO_URL.format(token=settings.telegram_bot_token)
    try:
        r = requests.post(
            url,
            data={"chat_id": settings.telegram_chat_id, "caption": caption[:1024]},
            files={"photo": ("daily.png", image_bytes, "image/png")},
            timeout=30,
        )
        if r.status_code == 200:
            return True
        logger.warning(f"telegram photo: HTTP {r.status_code} {r.text[:200]}")
    except requests.RequestException as e:
        logger.warning(f"telegram photo: {e}")
    return False


def send_daily_summary(*, as_of, nav: float, day_pnl: float, open_count: int,
                       fills: int, exits: dict, picks: list) -> bool:
    """Top-level send for the daily-tick summary.
    Renders an image card and sends as photo. Falls back to text on any failure."""
    try:
        png = render_daily_summary_image(
            as_of=as_of, nav=nav, day_pnl=day_pnl, open_count=open_count,
            fills=fills, exits=exits, picks=picks,
        )
        if send_telegram_photo(png):
            return True
        logger.warning("photo send failed, falling back to text")
    except Exception as e:
        logger.warning(f"image render failed, falling back to text: {e}")

    text_msg = format_daily_summary(
        as_of=as_of, nav=nav, day_pnl=day_pnl, open_count=open_count,
        fills=fills, exits=exits, picks=picks,
    )
    return send_telegram(text_msg)
