#!/usr/bin/env python3
"""
scripts/reconcile_okx.py

Reconcile OKX Order History CSV against the bot's trades table.

USAGE
-----
    python scripts/reconcile_okx.py --csv path/to/Order_History.csv \\
        [--db trading.db] [--match-window-sec 600] [--csv-out report.csv]

The matching/reporting logic lives in core/reconcile.py so the web
endpoint (/api/reconcile) can reuse it against live OKX REST data
without going through a CSV.
"""
from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
from datetime import datetime
from typing import List, Optional

# Make `core/reconcile.py` importable when run from the project root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.reconcile import (
    DBTrade, OKXOrder, MatchResult, reconcile, build_json_report,
)


# ────────────────────────────────────────────────────────────────────────
# CSV parsing — OKX Order History format
# ────────────────────────────────────────────────────────────────────────

def _strip_bom(s: str) -> str:
    return s.lstrip("﻿").strip() if s else s


def parse_okx_csv(path: str) -> List[OKXOrder]:
    """Parse an OKX Order History CSV. Skips spot-conversion + cancelled rows."""
    orders: List[OKXOrder] = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        first = f.readline()
        if "Order ID" not in first:
            reader = csv.reader(f)
        else:
            f.seek(0)
            reader = csv.reader(f)

        header: Optional[List[str]] = None
        for row in reader:
            if not row or not row[0].strip():
                continue
            cleaned = [_strip_bom(c) for c in row]
            if header is None and "Order ID" in cleaned:
                header = cleaned
                continue
            if header is None:
                continue
            if cleaned[0] == "Order ID":
                continue
            row_dict = dict(zip(header, cleaned))
            try:
                inst = row_dict.get("Instrument", "")
                symbol = row_dict.get("Symbol", "")
                if inst.lower() == "spot" or "CONVERT" in symbol.upper():
                    continue
                order = OKXOrder(
                    order_id=row_dict["Order ID"],
                    order_time=datetime.strptime(row_dict["Order Time"], "%Y-%m-%d %H:%M:%S"),
                    symbol=symbol,
                    side=row_dict.get("Side", ""),
                    order_type=row_dict.get("Order Type", ""),
                    filled_qty=float(row_dict.get("Filled Amount") or 0),
                    avg_fill_price=float(row_dict.get("Avg. Filled Price") or 0),
                    pnl=float(row_dict.get("PNL") or 0),
                    fee=float(row_dict.get("Fee") or 0),
                    status=row_dict.get("Status", ""),
                    raw=row_dict,
                )
                if order.status.upper() == "CANCELED" or order.filled_qty == 0:
                    continue
                orders.append(order)
            except (KeyError, ValueError) as e:
                print(f"WARNING: skipped malformed row: {e}", file=sys.stderr)
    return orders


def load_db_trades(db_path: str) -> List[DBTrade]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT id, position_type, entry_time, exit_time,
               entry_spot_price, entry_futures_price,
               exit_spot_price, exit_futures_price,
               quantity, pnl_usd, is_open
        FROM trades
        ORDER BY id DESC
    """)
    out: List[DBTrade] = []
    for r in cur.fetchall():
        out.append(DBTrade(
            id=r["id"],
            position_type=r["position_type"],
            entry_time=datetime.fromisoformat(r["entry_time"]) if r["entry_time"] else None,
            exit_time=datetime.fromisoformat(r["exit_time"]) if r["exit_time"] else None,
            entry_spot_price=r["entry_spot_price"] or 0.0,
            entry_futures_price=r["entry_futures_price"] or 0.0,
            exit_spot_price=r["exit_spot_price"] or 0.0,
            exit_futures_price=r["exit_futures_price"] or 0.0,
            quantity=r["quantity"] or 0.0,
            pnl_usd=r["pnl_usd"] or 0.0,
            is_open=bool(r["is_open"]),
        ))
    conn.close()
    return out


# ────────────────────────────────────────────────────────────────────────
# Console output
# ────────────────────────────────────────────────────────────────────────

def _fmt_time(t):
    return t.strftime("%Y-%m-%d %H:%M:%S") if t else "-"


def print_report(report: dict, matches, unmatched, csv_out: Optional[str] = None) -> int:
    s = report["summary"]
    print("=" * 78)
    print("OKX ↔ Bot DB Reconciliation Report")
    print("=" * 78)
    print()
    print(f"DB trades evaluated:        {s['db_trades_checked']}")
    print(f"  Fully matched on OKX:     {s['fully_matched']}")
    print(f"  Partial / missing legs:   {s['partial_or_missing']}")

    if report["partial_db_trades"]:
        print()
        print("─" * 78)
        print("DB TRADES WITH MISSING OKX LEGS")
        print("─" * 78)
        for p in report["partial_db_trades"]:
            print(
                f"  trade #{p['trade_id']} ({p['position_type']}) "
                f"{p['entry_time']} → {p['exit_time']} | "
                f"pnl=${p['pnl_usd']:+.2f} | missing: {', '.join(p['missing'])}"
            )

    if report["price_mismatches"]:
        print()
        print("─" * 78)
        print("PRICE MISMATCHES (DB vs OKX, > 1 bp)")
        print("─" * 78)
        for pm in report["price_mismatches"]:
            print(f"  trade #{pm['trade_id']} {pm['leg']:10s}  "
                  f"db={pm['db_price']:>12,.4f}  okx={pm['okx_price']:>12,.4f}  "
                  f"Δ={pm['diff_bps']:>6.2f} bps")

    if report["unmatched_okx"]:
        labels = {
            "market_orphan_close": "MARKET orphan close (auto-flatten by orphan detector)",
            "limit_no_db_record":  "LIMIT trade with no DB record (entry never persisted)",
            "market_no_db_record": "MARKET trade with no DB record (manual or auto-close)",
        }
        print()
        print("─" * 78)
        print(f"OKX ORDERS WITHOUT A DB MATCH ({s['unmatched_okx_orders']} total)")
        print("─" * 78)
        for key, g in report["unmatched_okx"].items():
            print(f"\n  ▸ {labels.get(key, key)}  "
                  f"[{g['count']} order(s), Σpnl={g['total_pnl']:+.2f}, Σfees={g['total_fees']:.2f}]")
            for o in g["orders"]:
                print(
                    f"     {o['order_time']}  {o['symbol']:25s}  "
                    f"{o['side']:14s} {o['order_type']:10s}  "
                    f"qty={o['filled_qty']:>7,.4f}  px={o['avg_fill_price']:>10,.4f}  "
                    f"pnl={o['pnl']:+7.2f}  fee={o['fee']:+7.4f}"
                )

    print()
    print("=" * 78)
    if s["issues_total"] == 0:
        print("✅ Clean: DB and OKX are aligned within tolerance.")
    else:
        print(f"⚠️  {s['issues_total']} reconciliation issue(s) found.")
    print("=" * 78)

    if csv_out:
        _write_csv(csv_out, matches, unmatched)
        print(f"\nDetailed CSV written to: {csv_out}")

    return s["issues_total"]


def _write_csv(path, matches, unmatched):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "category", "db_trade_id", "db_position_type",
            "db_entry_time", "db_exit_time", "db_pnl_usd",
            "okx_order_id", "okx_order_time", "okx_symbol", "okx_side",
            "okx_order_type", "okx_filled_qty", "okx_avg_fill_price",
            "okx_pnl", "okx_fee", "okx_status", "notes",
        ])
        for m in matches:
            if m.is_full_match:
                continue
            t = m.db_trade
            for label, o in (
                ("missing-open-eth",  m.okx_open_eth),
                ("missing-open-btc",  m.okx_open_btc),
                ("missing-close-eth", m.okx_close_eth),
                ("missing-close-btc", m.okx_close_btc),
            ):
                if o is None:
                    w.writerow([
                        label, t.id, t.position_type,
                        _fmt_time(t.entry_time), _fmt_time(t.exit_time), t.pnl_usd,
                        "", "", "", "", "", "", "", "", "", "",
                        "no OKX order found within window",
                    ])
        for o in unmatched:
            w.writerow([
                "okx-unmatched", "", "",
                "", "", "",
                o.order_id, _fmt_time(o.order_time), o.symbol, o.side,
                o.order_type, o.filled_qty, o.avg_fill_price,
                o.pnl, o.fee, o.status,
                "no DB trade claims this order",
            ])


# ────────────────────────────────────────────────────────────────────────
# Entrypoint
# ────────────────────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--csv", required=True, help="OKX Order History CSV path")
    p.add_argument("--db",  default="trading.db", help="Bot SQLite DB path")
    p.add_argument("--match-window-sec", type=int, default=600,
                   help="Max seconds between DB and OKX times for a match (default: 600)")
    p.add_argument("--price-tol-bps", type=float, default=10.0,
                   help="Max price difference (bps) for a match (default: 10)")
    p.add_argument("--csv-out", help="Write detailed reconciliation CSV here")
    args = p.parse_args(argv)

    if not os.path.exists(args.csv):
        print(f"ERROR: CSV not found: {args.csv}", file=sys.stderr); return 2
    if not os.path.exists(args.db):
        print(f"ERROR: DB not found: {args.db}", file=sys.stderr); return 2

    print(f"Loading OKX CSV: {args.csv}")
    okx_orders = parse_okx_csv(args.csv)
    print(f"  {len(okx_orders)} filled order(s) parsed")

    print(f"Loading bot DB:  {args.db}")
    db_trades = load_db_trades(args.db)
    closed = [t for t in db_trades if not t.is_open]
    print(f"  {len(closed)} closed trade(s) ({len(db_trades) - len(closed)} still open)")

    matches, unmatched = reconcile(
        okx_orders, db_trades,
        window_sec=args.match_window_sec,
        price_tol_bps=args.price_tol_bps,
    )
    report = build_json_report(matches, unmatched)
    issues = print_report(report, matches, unmatched, csv_out=args.csv_out)
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())
