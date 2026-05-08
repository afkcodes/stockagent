"""News scraper — Moneycontrol + Google News for a given NSE symbol.

Used by the sentiment agent. Pulls the last N news items, persists raw to `news`
table, returns lightweight items for the LLM to read.

We deliberately use RSS-style endpoints / public search rather than paid APIs so
this stays in the "free" lane.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup
from loguru import logger
from sqlalchemy import text
from tenacity import retry, stop_after_attempt, wait_exponential

from stockagent.db.session import get_engine

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Accept": "application/rss+xml,application/xml;q=0.9,*/*;q=0.8",
}


@dataclass
class NewsItem:
    symbol: str
    source: str
    url: str
    title: str
    published_at: datetime | None
    body: str = ""


@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=5), reraise=True)
def _fetch(url: str, timeout: int = 12) -> str | None:
    try:
        r = requests.get(url, headers=_HEADERS, timeout=timeout)
        if r.status_code == 200:
            return r.text
        logger.warning(f"news fetch {url}: HTTP {r.status_code}")
    except requests.RequestException as e:
        logger.warning(f"news fetch {url}: {e}")
    return None


def fetch_google_news(symbol: str, max_items: int = 8) -> list[NewsItem]:
    """Google News RSS for the symbol. Stable, free, well-structured."""
    query = f'"{symbol}" stock NSE'
    url = f"https://news.google.com/rss/search?q={quote(query)}&hl=en-IN&gl=IN&ceid=IN:en"
    body = _fetch(url)
    if not body:
        return []
    soup = BeautifulSoup(body, "xml")
    items = []
    for it in soup.find_all("item")[:max_items]:
        title = it.title.get_text(strip=True) if it.title else ""
        link = it.link.get_text(strip=True) if it.link else ""
        pub = None
        if it.pubDate:
            try:
                pub = datetime.strptime(it.pubDate.get_text(strip=True), "%a, %d %b %Y %H:%M:%S %Z")
                pub = pub.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                pub = None
        body_txt = ""
        if it.description:
            body_txt = re.sub(r"<[^>]+>", " ", it.description.get_text())[:600]
        items.append(NewsItem(symbol=symbol.upper(), source="google_news",
                              url=link, title=title, published_at=pub, body=body_txt))
    return items


def fetch_moneycontrol_news(symbol: str, max_items: int = 5) -> list[NewsItem]:
    """Moneycontrol search-results page. HTML scrape; less reliable than Google News."""
    url = f"https://www.moneycontrol.com/news/tags/{symbol.lower()}.html"
    body = _fetch(url)
    if not body:
        return []
    soup = BeautifulSoup(body, "lxml")
    items = []
    for a in soup.select("li.clearfix h2 a")[:max_items]:
        link = a.get("href", "")
        title = a.get_text(strip=True)
        if title and link:
            items.append(NewsItem(symbol=symbol.upper(), source="moneycontrol",
                                  url=link, title=title, published_at=None))
    return items


def fetch_recent_news(symbol: str, max_items: int = 10) -> list[NewsItem]:
    """Combine sources, dedupe by URL, return most-recent first."""
    items = []
    try:
        items += fetch_google_news(symbol, max_items=8)
    except Exception as e:
        logger.warning(f"google_news for {symbol}: {e}")
    try:
        items += fetch_moneycontrol_news(symbol, max_items=5)
    except Exception as e:
        logger.warning(f"moneycontrol for {symbol}: {e}")

    seen = set()
    out = []
    for it in items:
        if it.url and it.url not in seen:
            seen.add(it.url)
            out.append(it)
    out.sort(key=lambda x: x.published_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return out[:max_items]


def persist_news(items: list[NewsItem]) -> int:
    if not items:
        return 0
    sql = text(
        """
        INSERT OR IGNORE INTO news (symbol, source, url, title, published_at, body)
        VALUES (:symbol, :source, :url, :title, :pub, :body)
        """
    )
    rows = [{
        "symbol": it.symbol, "source": it.source, "url": it.url,
        "title": it.title[:500],
        "pub": it.published_at.isoformat() if it.published_at else None,
        "body": it.body[:2000],
    } for it in items if it.url]
    with get_engine().begin() as c:
        c.execute(sql, rows)
    return len(rows)
