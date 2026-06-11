"""
Pull 6 months of 1-minute OKX candles for backtest pairs.

Public endpoint only — no API key required:
    GET /api/v5/market/history-candles?instId=...&bar=1m&before=<ms>&limit=300

OKX returns at most 300 candles per request, going backwards from `before`.
For 6 months × 1-min = ~262k candles per instrument.
At ~5 requests/sec sustained anon limit, expect ~3 min per instrument,
~50 min total for 8 pairs × 2 legs.

Usage:
    python -m backtest.fetch_history --days 180 --out backtest/data
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

import aiohttp
import pandas as pd

logger = logging.getLogger(__name__)

OKX_BASE = "https://www.okx.com"
CANDLES_ENDPOINT = "/api/v5/market/history-candles"
INSTRUMENTS_ENDPOINT = "/api/v5/public/instruments"

# 8 candidate pairs chosen for: high liquidity, established mean-reverting
# behavior between spot and perp, varied beta to BTC.
DEFAULT_PAIRS = ["ETH", "SOL", "AVAX", "LINK", "MATIC", "DOT", "ATOM", "NEAR"]

# OKX history-candles is rate-limited to 20 req / 2s anonymous.
# Stay well under to leave headroom for retries.
REQ_PER_SEC = 5
SLEEP_PER_REQ = 1.0 / REQ_PER_SEC

CANDLE_COLS = ["ts", "o", "h", "l", "c", "vol", "volCcy", "volCcyQuote", "confirm"]


@dataclass
class Instrument:
    symbol: str             # e.g. "ETH-USDT" or "ETH-USDT-SWAP"
    inst_type: str          # "SPOT" or "SWAP"
    base: str               # "ETH"
    ct_val: float = 0.0     # contract size in base for SWAP; 0 for SPOT
    min_sz: float = 0.0
    lot_sz: float = 0.0


async def _get_json(session: aiohttp.ClientSession, path: str,
                    params: dict, retries: int = 3) -> dict:
    url = OKX_BASE + path
    for attempt in range(retries):
        try:
            async with session.get(url, params=params, timeout=15) as resp:
                payload = await resp.json()
                code = payload.get("code", "")
                if code == "0":
                    return payload
                if code == "50013":
                    wait = 2 ** attempt
                    logger.warning("OKX rate-limited (50013) %s — retry in %ds",
                                   params.get("instId"), wait)
                    await asyncio.sleep(wait)
                    continue
                logger.warning("OKX %s error code=%s msg=%s",
                               path, code, payload.get("msg"))
                return payload
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            wait = 2 ** attempt
            logger.warning("Network error on %s: %s — retry in %ds",
                           url, exc, wait)
            await asyncio.sleep(wait)
    return {"code": "fail", "data": []}


async def fetch_instrument_meta(session: aiohttp.ClientSession,
                                base: str) -> List[Instrument]:
    """Fetch metadata for {base}-USDT (SPOT) and {base}-USDT-SWAP."""
    instruments: List[Instrument] = []
    for inst_type, symbol in [("SPOT", f"{base}-USDT"),
                              ("SWAP", f"{base}-USDT-SWAP")]:
        params = {"instType": inst_type, "instId": symbol}
        payload = await _get_json(session, INSTRUMENTS_ENDPOINT, params)
        data = payload.get("data") or []
        if not data:
            logger.warning("No instrument meta for %s", symbol)
            continue
        row = data[0]
        instruments.append(Instrument(
            symbol=symbol,
            inst_type=inst_type,
            base=base,
            ct_val=float(row.get("ctVal") or 0),
            min_sz=float(row.get("minSz") or 0),
            lot_sz=float(row.get("lotSz") or 0),
        ))
    return instruments


async def fetch_candles_for_symbol(session: aiohttp.ClientSession,
                                   symbol: str,
                                   days: int) -> pd.DataFrame:
    """Pull 1-minute candles for one instrument, going back `days` from now."""
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 24 * 60 * 60 * 1000
    all_rows: list = []

    cursor = end_ms
    pages = 0
    started_at = time.monotonic()

    while cursor > start_ms:
        params = {
            "instId": symbol,
            "bar": "1m",
            "limit": "300",
            "before": "",        # OKX is finicky; use only `after` for pagination
            "after": str(cursor),
        }
        payload = await _get_json(session, CANDLES_ENDPOINT, params)
        rows = payload.get("data") or []
        if not rows:
            break

        # OKX returns DESC by ts. Earliest row is rows[-1].
        all_rows.extend(rows)
        oldest_ts = int(rows[-1][0])
        if oldest_ts >= cursor:
            break  # no progress, prevent infinite loop
        cursor = oldest_ts
        pages += 1

        if pages % 20 == 0:
            elapsed = time.monotonic() - started_at
            covered_days = (end_ms - cursor) / (24 * 60 * 60 * 1000)
            logger.info("  %s: %d pages, %.1f/%d days, %.1fs elapsed",
                        symbol, pages, covered_days, days, elapsed)

        await asyncio.sleep(SLEEP_PER_REQ)

    if not all_rows:
        return pd.DataFrame(columns=CANDLE_COLS)

    df = pd.DataFrame(all_rows, columns=CANDLE_COLS)
    df["ts"] = pd.to_datetime(df["ts"].astype("int64"), unit="ms", utc=True)
    for c in ("o", "h", "l", "c", "vol", "volCcy", "volCcyQuote"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)
    df = df[df["ts"] >= pd.Timestamp(start_ms, unit="ms", tz="UTC")]
    return df[["ts", "o", "h", "l", "c", "vol"]]


async def fetch_pair(session: aiohttp.ClientSession,
                     base: str,
                     days: int,
                     out_dir: Path) -> dict:
    """Fetch spot + perp 1-min OHLC for one base asset, save as parquet."""
    logger.info("=== %s ===", base)
    instruments = await fetch_instrument_meta(session, base)
    if len(instruments) != 2:
        logger.error("Skipping %s — could not resolve both spot+perp meta", base)
        return {"base": base, "ok": False, "reason": "meta_missing"}

    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {"base": base, "instruments": [], "ok": True}

    for inst in instruments:
        logger.info("Fetching %s (%s)…", inst.symbol, inst.inst_type)
        df = await fetch_candles_for_symbol(session, inst.symbol, days)
        if df.empty:
            logger.warning("No data returned for %s", inst.symbol)
            summary["ok"] = False
            continue
        path = out_dir / f"{inst.symbol}.parquet"
        df.to_parquet(path, index=False)
        meta_path = out_dir / f"{inst.symbol}.meta.json"
        meta_path.write_text(
            f'{{"symbol":"{inst.symbol}","inst_type":"{inst.inst_type}",'
            f'"base":"{inst.base}","ct_val":{inst.ct_val},'
            f'"min_sz":{inst.min_sz},"lot_sz":{inst.lot_sz},'
            f'"rows":{len(df)},'
            f'"first_ts":"{df["ts"].iloc[0].isoformat()}",'
            f'"last_ts":"{df["ts"].iloc[-1].isoformat()}"}}'
        )
        summary["instruments"].append({
            "symbol": inst.symbol,
            "rows": len(df),
            "first": df["ts"].iloc[0].isoformat(),
            "last": df["ts"].iloc[-1].isoformat(),
        })
        logger.info("  saved %d rows → %s", len(df), path.name)

    return summary


async def main_async(pairs: List[str], days: int, out_dir: Path) -> None:
    timeout = aiohttp.ClientTimeout(total=None, sock_read=30, sock_connect=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        all_summaries = []
        for base in pairs:
            try:
                summary = await fetch_pair(session, base, days, out_dir)
                all_summaries.append(summary)
            except Exception:
                logger.exception("Failed to fetch %s — continuing", base)

    print()
    print("=" * 60)
    print(f"Fetch complete — {len(all_summaries)} pairs processed")
    print("=" * 60)
    for s in all_summaries:
        ok = "✓" if s.get("ok") else "✗"
        for inst in s.get("instruments", []):
            print(f"  {ok} {inst['symbol']:<22} rows={inst['rows']:>7}  "
                  f"{inst['first'][:10]} → {inst['last'][:10]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pairs", nargs="+", default=DEFAULT_PAIRS,
                        help="Base assets (default: 8 candidate pairs)")
    parser.add_argument("--days", type=int, default=180,
                        help="Days of history to pull (default: 180)")
    parser.add_argument("--out", type=Path, default=Path("backtest/data"),
                        help="Output directory for parquet files")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(main_async(args.pairs, args.days, args.out))


if __name__ == "__main__":
    main()
