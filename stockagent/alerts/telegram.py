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
    """Render the daily summary as a clean PNG with a real table for picks.
    Returns image bytes. Monospace + headers + row separators. No fancy styling."""
    from PIL import Image, ImageDraw

    starting = settings.capital_inr
    ret_pct = (nav - starting) / starting * 100
    black = (0, 0, 0)
    grey = (90, 90, 90)
    light_grey = (220, 220, 220)
    pnl_color = (16, 124, 16) if day_pnl >= 0 else (192, 32, 32)

    title_font = _find_font(16, bold=True)
    section_font = _find_font(12, bold=True)
    header_font = _find_font(10, bold=True)
    body_font = _find_font(10, mono=True)
    body_bold = _find_font(11, mono=True, bold=True)

    pad = 14
    line_h = 17
    row_h = 20

    # Column layout — (header, width_px, align). Tightened for compactness.
    cols = [
        ("#",       22, "right"),
        ("Symbol",  92, "left"),
        ("Sector",  70, "left"),
        ("Entry",   70, "right"),
        ("Stop",    70, "right"),
        ("Target",  70, "right"),
        ("Qty",     42, "right"),
        ("Alloc",   72, "right"),
        ("Conv",    42, "right"),
    ]
    col_gap = 8
    table_width = sum(w for _, w, _ in cols) + col_gap * (len(cols) - 1)
    width = pad * 2 + table_width

    # Pre-compute column x ranges
    col_ranges: list[tuple[int, int, str]] = []  # (x_start, x_end, align)
    x = pad
    for _, w, align in cols:
        col_ranges.append((x, x + w, align))
        x += w + col_gap

    def _draw_cell(draw, y, text, font, color, x_start, x_end, align):
        try:
            tw = draw.textlength(text, font=font)
        except AttributeError:
            tw = font.getmask(text).size[0] if hasattr(font, "getmask") else len(text) * 7
        if align == "right":
            tx = x_end - tw
        elif align == "center":
            tx = x_start + (x_end - x_start - tw) / 2
        else:
            tx = x_start
        draw.text((tx, y), text, fill=color, font=font)

    # Compute total height
    header_h = pad + line_h * 4 + 10  # title + 3 summary lines + bottom margin
    if picks:
        body_h = line_h + 4 + row_h + 4 + len(picks) * row_h + pad
    else:
        body_h = line_h + pad
    height = header_h + body_h

    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)

    # ─── Top: title + summary block ──────────────────────────────────────
    y = pad
    draw.text((pad, y), f"stockagent daily  -  {as_of}", fill=black, font=title_font)
    y += line_h + 4
    draw.text((pad, y), f"NAV  Rs {nav:>11,.0f}    ({ret_pct:+.2f}% from start)",
              fill=black, font=body_font)
    y += line_h
    draw.text((pad, y), f"Day P&L  Rs {day_pnl:>+11,.0f}",
              fill=pnl_color, font=body_bold)
    y += line_h
    draw.text((pad, y),
              f"Open:{open_count}  Fills:{fills}  Exits  stop:{exits.get('stop',0)}  signal:{exits.get('signal',0)}  time:{exits.get('time',0)}",
              fill=grey, font=body_font)
    y += line_h + 8

    if not picks:
        draw.text((pad, y), "No qualifying signals today.", fill=grey, font=body_font)
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()

    # ─── Section title + table ────────────────────────────────────────────
    draw.text((pad, y), f"Tomorrow's watchlist  ({len(picks)} picks)",
              fill=black, font=section_font)
    y += line_h + 4

    # Table header row
    for (header, _, _), (x_start, x_end, align) in zip(cols, col_ranges):
        _draw_cell(draw, y, header, header_font, black, x_start, x_end, align)
    y += row_h - 4

    # Header underline
    draw.line([(pad, y), (pad + table_width, y)], fill=black, width=1)
    y += 6

    # Data rows
    for i, p in enumerate(picks):
        sector = (getattr(p, "sector", "-") or "-")[:12]
        row = [
            str(i + 1),
            p.symbol,
            sector,
            f"{p.entry:,.2f}",
            f"{p.stop:,.2f}",
            f"{p.target:,.2f}",
            str(p.qty),
            f"{p.position_size_inr:,.0f}",
            f"{p.conviction:.2f}",
        ]
        for j, (cell, (x_start, x_end, align)) in enumerate(zip(row, col_ranges)):
            # Symbol bold; numbers in grey for visual hierarchy
            if j == 1:  # Symbol col
                font = body_bold
                color = black
            elif j == 0:  # # col
                font = body_font
                color = grey
            else:
                font = body_font
                color = black if j == 8 else grey  # conviction stays black for emphasis
            _draw_cell(draw, y, cell, font, color, x_start, x_end, align)
        y += row_h
        # Light row separator
        if i < len(picks) - 1:
            draw.line([(pad, y - 3), (pad + table_width, y - 3)],
                      fill=light_grey, width=1)

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
