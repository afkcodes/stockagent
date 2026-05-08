"""Symbol → sector lookup. Built from NSE's sector indices in nselib.

Used by the coordinator's sector-concentration cap. We populate the mapping once
(idempotent UPSERT) and refresh quarterly. The data lives in a stocks table — for
simplicity we store it as JSON on a single config row instead of a new table.
"""
from __future__ import annotations

import json
from typing import Iterable

from loguru import logger
from sqlalchemy import text

from stockagent.db.session import get_engine

# NSE sector indices that constituent_stock_list can resolve.
_SECTOR_INDICES = [
    "Nifty Auto", "Nifty Bank", "Nifty Financial Services",
    "Nifty FMCG", "Nifty IT", "Nifty Media", "Nifty Metal", "Nifty Pharma",
    "Nifty PSU Bank", "Nifty Private Bank", "Nifty Realty", "Nifty Healthcare",
    "Nifty Consumer Durables", "Nifty Oil and Gas", "Nifty Chemicals",
]


def refresh_sector_map() -> dict[str, str]:
    """Build symbol → sector mapping from NSE sector indices. Stores in `config` table."""
    try:
        from nselib import indices
    except ImportError:
        logger.warning("nselib not available; cannot build sector map")
        return {}

    sym_to_sector: dict[str, str] = {}
    for idx_name in _SECTOR_INDICES:
        try:
            df = indices.constituent_stock_list(index_category="SectoralIndices", index_name=idx_name)
            if df is None or df.empty:
                continue
            sector = idx_name.replace("Nifty ", "").strip()
            col = next((c for c in df.columns if c.lower() == "symbol"), df.columns[0])
            for s in df[col].dropna().astype(str).str.strip():
                sym_to_sector.setdefault(s.upper(), sector)
        except Exception as e:
            logger.warning(f"sector fetch {idx_name} failed: {e}")

    if sym_to_sector:
        with get_engine().begin() as c:
            c.execute(text(
                "INSERT INTO config (key, value) VALUES (:k, :v) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            ), {"k": "sector_map", "v": json.dumps(sym_to_sector)})
        logger.info(f"sector map updated: {len(sym_to_sector)} symbols across {len(_SECTOR_INDICES)} indices")
    return sym_to_sector


def get_sector_map() -> dict[str, str]:
    """Read the cached sector map. Returns {} if not built yet."""
    with get_engine().connect() as c:
        row = c.execute(text("SELECT value FROM config WHERE key = 'sector_map'")).scalar()
    if not row:
        return {}
    try:
        return json.loads(row)
    except json.JSONDecodeError:
        return {}


def sector_for(symbol: str) -> str:
    """Lookup. Returns 'Other' if not in any tracked sector index."""
    return get_sector_map().get(symbol.upper(), "Other")
