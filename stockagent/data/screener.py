"""Scrape fundamental ratios from screener.in for a given NSE symbol.

Used by the fundamental agent. Idempotent — UPSERTs into the `fundamentals` table
with `as_of_date` so we can track how a stock's fundamentals evolved.

screener.in is the de-facto retail fundamentals source for Indian equities. The
endpoints are HTML, no API. We scrape the company page and extract the ratio block.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date

import requests
from bs4 import BeautifulSoup
from loguru import logger
from sqlalchemy import text
from tenacity import retry, stop_after_attempt, wait_exponential

from stockagent.db.session import get_engine

_BASE_URL = "https://www.screener.in/company/{symbol}/consolidated/"
_FALLBACK_URL = "https://www.screener.in/company/{symbol}/"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_RATIO_LABEL_MAP = {
    "Market Cap": "market_cap",
    "Stock P/E": "pe",
    "P/E": "pe",
    "Price to book value": "pb",
    "Book Value": "book_value",
    "Dividend Yield": "div_yield",
    "ROCE": "roce",
    "ROE": "roe",
    "Debt to equity": "debt_equity",
    "Debt": "total_debt",
    "PEG Ratio": "peg",
    "Promoter holding": "promoter_holding",
    "Pledged percentage": "pledged_pct",
    "Sales growth": "sales_growth",
    "Profit growth": "profit_growth",
    "Sales growth 3Years": "sales_growth_3y",
    "Profit growth 3Years": "profit_growth_3y",
    "Sales growth 5Years": "sales_growth_5y",
    "Profit growth 5Years": "profit_growth_5y",
}


@dataclass
class Fundamentals:
    symbol: str
    as_of_date: date
    market_cap: float | None
    pe: float | None
    peg: float | None
    pb: float | None
    roe: float | None
    roce: float | None
    debt_equity: float | None
    promoter_holding: float | None
    pledged_pct: float | None
    sales_growth_3y: float | None
    profit_growth_3y: float | None
    raw: dict

    def red_flags(self) -> list[str]:
        """Mechanical quality checks. Empty list = clean fundamentals."""
        flags: list[str] = []
        # High debt
        if self.debt_equity is not None and self.debt_equity > 1.5:
            flags.append(f"high_debt_equity={self.debt_equity:.2f}")
        # Pledged shares (promoter pledging is a major red flag)
        if self.pledged_pct is not None and self.pledged_pct > 20:
            flags.append(f"high_pledged_pct={self.pledged_pct:.1f}%")
        # Negative profit growth (declining business)
        if self.profit_growth_3y is not None and self.profit_growth_3y < -10:
            flags.append(f"profit_decline_3y={self.profit_growth_3y:.1f}%")
        # Negative sales growth
        if self.sales_growth_3y is not None and self.sales_growth_3y < -5:
            flags.append(f"sales_decline_3y={self.sales_growth_3y:.1f}%")
        # Very low promoter holding (often a sign of low conviction or distress)
        if self.promoter_holding is not None and self.promoter_holding < 25:
            flags.append(f"low_promoter_holding={self.promoter_holding:.1f}%")
        # Crazy PE (overvalued or earnings collapsed)
        if self.pe is not None and (self.pe > 150 or self.pe < 0):
            flags.append(f"abnormal_pe={self.pe:.1f}")
        # ROE check
        if self.roe is not None and self.roe < 5:
            flags.append(f"low_roe={self.roe:.1f}%")
        return flags

    def quality_score(self) -> float:
        """0..1 quality score based on standard ratios. Heuristic; used by fundamental agent."""
        score = 0.5  # neutral start
        weights_added = 0.0
        if self.roe is not None:
            score += (min(self.roe, 30) - 12) / 100  # 12% ROE neutral, +0.18 at 30%
            weights_added += 1
        if self.roce is not None:
            score += (min(self.roce, 30) - 14) / 100
            weights_added += 1
        if self.debt_equity is not None:
            score -= max(0, self.debt_equity - 0.5) * 0.1
            weights_added += 1
        if self.profit_growth_3y is not None:
            score += min(self.profit_growth_3y, 40) / 200  # 20% growth → +0.10
            weights_added += 1
        if self.sales_growth_3y is not None:
            score += min(self.sales_growth_3y, 25) / 250
            weights_added += 1
        if self.pledged_pct is not None and self.pledged_pct > 0:
            score -= min(self.pledged_pct, 50) / 100
        return max(0.0, min(1.0, score))


def _parse_number(text: str) -> float | None:
    """Convert screener-formatted numbers to float. Handles ₹ symbol, %, commas, Cr."""
    if not text:
        return None
    s = text.strip().replace(",", "").replace("₹", "").replace("\xa0", " ").strip()
    s = s.replace("%", "").strip()
    if s in ("", "-", "—"):
        return None
    multiplier = 1.0
    if s.endswith(" Cr."):
        s = s[: -len(" Cr.")].strip()
    if s.endswith(" Cr"):
        s = s[: -len(" Cr")].strip()
    if s.endswith(" Days") or s.endswith(" days"):
        return None  # not a number we care about
    try:
        return float(s) * multiplier
    except ValueError:
        return None


@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=5), reraise=True)
def _fetch_html(symbol: str) -> str | None:
    s = requests.Session()
    s.headers.update(_HEADERS)
    for url_tmpl in (_BASE_URL, _FALLBACK_URL):
        url = url_tmpl.format(symbol=symbol.upper())
        try:
            r = s.get(url, timeout=15)
            if r.status_code == 200 and "company-info" in r.text or "Stock P/E" in r.text:
                return r.text
        except requests.RequestException as e:
            logger.warning(f"screener fetch {url}: {e}")
    return None


def fetch_fundamentals(symbol: str) -> Fundamentals | None:
    """Scrape screener.in for `symbol`. Returns None if unavailable."""
    html = _fetch_html(symbol)
    if not html:
        return None

    soup = BeautifulSoup(html, "lxml")
    raw: dict[str, float | None] = {}

    # screener.in current structure (verified 2026-05):
    #   <li> <span class="name">Market Cap</span>
    #        <span class="value">₹ <span class="number">19,42,866</span> Cr.</span>
    #   </li>
    for li in soup.select("ul#top-ratios li"):
        name_el = li.find("span", class_="name")
        if not name_el:
            continue
        label = name_el.get_text(strip=True)
        if label not in _RATIO_LABEL_MAP:
            continue
        # The .number span is the cleanest place to read the numeric value
        num_el = li.find("span", class_="number")
        if num_el:
            val = _parse_number(num_el.get_text(strip=True))
        else:
            # Fall back to whatever non-label text remains
            value_el = li.find("span", class_="value") or li.find("span", class_="nowrap")
            val = _parse_number(value_el.get_text(" ", strip=True)) if value_el else None
        if val is not None:
            raw[_RATIO_LABEL_MAP[label]] = val

    # Promoter / pledged data lives in a separate table
    for section in soup.select("section.shareholding-section"):
        text_blob = section.get_text(" ", strip=True)
        m = re.search(r"Promoters\s*([\d.]+)\s*%", text_blob)
        if m and "promoter_holding" not in raw:
            raw["promoter_holding"] = float(m.group(1))

    # Growth ratios from the "Compounded Sales/Profit Growth" section
    for table in soup.select("table.ranges-table"):
        rows = table.find_all("tr")
        for row in rows:
            cells = row.find_all("td")
            if len(cells) >= 2:
                label = cells[0].get_text(strip=True)
                val = _parse_number(cells[1].get_text())
                if "Sales Growth" in label and "3 Years" in label:
                    raw["sales_growth_3y"] = val
                elif "Profit Growth" in label and "3 Years" in label:
                    raw["profit_growth_3y"] = val

    f = Fundamentals(
        symbol=symbol.upper(),
        as_of_date=date.today(),
        market_cap=raw.get("market_cap"),
        pe=raw.get("pe"),
        peg=raw.get("peg"),
        pb=raw.get("pb"),
        roe=raw.get("roe"),
        roce=raw.get("roce"),
        debt_equity=raw.get("debt_equity"),
        promoter_holding=raw.get("promoter_holding"),
        pledged_pct=raw.get("pledged_pct"),
        sales_growth_3y=raw.get("sales_growth_3y"),
        profit_growth_3y=raw.get("profit_growth_3y"),
        raw=raw,
    )
    return f


def upsert_fundamentals(f: Fundamentals) -> None:
    sql = text(
        """
        INSERT INTO fundamentals (symbol, as_of_date, market_cap, pe, peg, pb, roe, roce,
                                   debt_equity, promoter_holding, pledged_pct,
                                   sales_growth_3y, profit_growth_3y, raw_json)
        VALUES (:symbol, :as_of, :mc, :pe, :peg, :pb, :roe, :roce,
                :de, :ph, :pp, :sg, :pg, :raw)
        ON CONFLICT(symbol, as_of_date) DO UPDATE SET
            market_cap=excluded.market_cap, pe=excluded.pe, peg=excluded.peg,
            pb=excluded.pb, roe=excluded.roe, roce=excluded.roce,
            debt_equity=excluded.debt_equity, promoter_holding=excluded.promoter_holding,
            pledged_pct=excluded.pledged_pct, sales_growth_3y=excluded.sales_growth_3y,
            profit_growth_3y=excluded.profit_growth_3y, raw_json=excluded.raw_json
        """
    )
    with get_engine().begin() as c:
        c.execute(sql, {
            "symbol": f.symbol, "as_of": str(f.as_of_date),
            "mc": f.market_cap, "pe": f.pe, "peg": f.peg, "pb": f.pb,
            "roe": f.roe, "roce": f.roce, "de": f.debt_equity,
            "ph": f.promoter_holding, "pp": f.pledged_pct,
            "sg": f.sales_growth_3y, "pg": f.profit_growth_3y,
            "raw": json.dumps(f.raw),
        })


def latest_fundamentals(symbol: str, max_age_days: int = 60) -> Fundamentals | None:
    """Read the most-recent fundamentals row from DB if it's not too stale.
    Returns None if missing or older than max_age_days (caller should re-fetch then)."""
    from datetime import datetime, timedelta
    sql = text(
        "SELECT * FROM fundamentals WHERE symbol = :s "
        "ORDER BY as_of_date DESC LIMIT 1"
    )
    with get_engine().connect() as c:
        row = c.execute(sql, {"s": symbol.upper()}).mappings().first()
    if not row:
        return None
    as_of = datetime.fromisoformat(row["as_of_date"]).date() if isinstance(row["as_of_date"], str) else row["as_of_date"]
    if (date.today() - as_of).days > max_age_days:
        return None
    return Fundamentals(
        symbol=row["symbol"], as_of_date=as_of,
        market_cap=row["market_cap"], pe=row["pe"], peg=row["peg"], pb=row["pb"],
        roe=row["roe"], roce=row["roce"], debt_equity=row["debt_equity"],
        promoter_holding=row["promoter_holding"], pledged_pct=row["pledged_pct"],
        sales_growth_3y=row["sales_growth_3y"], profit_growth_3y=row["profit_growth_3y"],
        raw=json.loads(row["raw_json"]) if row["raw_json"] else {},
    )


def get_or_fetch(symbol: str, max_age_days: int = 60) -> Fundamentals | None:
    """Cache-first read. Refresh from screener.in if missing or stale."""
    cached = latest_fundamentals(symbol, max_age_days=max_age_days)
    if cached:
        return cached
    fresh = fetch_fundamentals(symbol)
    if fresh:
        upsert_fundamentals(fresh)
    return fresh
