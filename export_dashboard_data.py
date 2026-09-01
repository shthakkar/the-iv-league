#!/usr/bin/env python3
# ================================================================
# DASHBOARD DATA EXPORT — spec §29 / docs/superpowers/specs/
# 2026-08-31-hackathon-dashboard-design.md.
#
# Reads every logs/<date>-execution.log the Execution Agent has written,
# reconstructs each day's trades (ENTRY+EXIT joined by symbol), computes
# per-day and per-strategy aggregates, and writes it all -- plus a live
# account snapshot -- to dashboard/data.json for the static dashboard
# page to render.
#
# Run manually for now:
#   venv/bin/python3 export_dashboard_data.py
# This is the script a future GitHub Actions workflow will call on a
# schedule (not built yet -- see NEXTSTEPS.md).
#
# Placement note: lives at repo root, not scripts/, despite the design
# doc naming scripts/export_dashboard_data.py -- every other unit-tested
# module (risk_manager.py, strategy_engine.py, execution_agent.py,
# black_scholes.py) lives at root so tests can `import <module>` with no
# sys.path hack; scripts/ is shell orchestration plus one untested CLI
# utility (save_mock_fixture.py). Matched that convention since this
# module's parsing/aggregation logic is unit-tested the same way.
# ================================================================
from __future__ import annotations

import datetime
import json
import pathlib
import re
from collections import defaultdict

import risk_manager

REPO_ROOT = pathlib.Path(__file__).resolve().parent
LOGS_DIR = REPO_ROOT / "logs"
DASHBOARD_DATA_PATH = REPO_ROOT / "dashboard" / "data.json"

# Real starting capital for this paper account (spec §7). Not derivable
# from any log -- recorded once here rather than re-fetched.
STARTING_BALANCE = 100_000.00

STRATEGY_VERSION = "v1.0"

EXIT_REASON_MAP = {
    "TP": "take_profit",
    "SL": "stop_loss",
    "EOD": "eod",
    "TIME": "time_exit_1430",
}

_LOG_FILENAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-execution\.log$")


def option_type_from_symbol(symbol: str) -> str:
    """OCC-style option symbol: root + YYMMDD + C|P + 8-digit strike.
    The strike is always exactly 8 digits, so the C/P flag is always the
    9th-from-last character -- e.g. "SPY260831P00765000" -> "P"."""
    return symbol[-9]


def parse_execution_log(path: pathlib.Path) -> tuple[list[dict], list[dict]]:
    """Parses one logs/<date>-execution.log into (entries, exits). Each
    line is space-delimited (str.split() collapses the ENTRY/EXIT column's
    padding automatically), e.g.:
      ENTRY 2026-08-31T10:35:37.611971-04:00 TSLA TSLA260831P00357500 qty=1 price=0.50 side=SELL_TO_OPEN
      EXIT  2026-08-31T10:51:44.766291-04:00 AAPL AAPL260831P00312500 qty=1 price=0.16 reason=TP
    """
    entries: list[dict] = []
    exits: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        record_type, timestamp, ticker, symbol = parts[0], parts[1], parts[2], parts[3]
        kv = dict(p.split("=", 1) for p in parts[4:])
        base = {
            "timestamp": timestamp, "ticker": ticker, "symbol": symbol,
            "quantity": int(kv["qty"]), "price": float(kv["price"]),
        }
        if record_type == "ENTRY":
            entries.append({**base, "side": kv["side"]})
        elif record_type == "EXIT":
            exits.append({**base, "reason": kv["reason"]})
    return entries, exits


def reconstruct_trades(entries: list[dict], exits: list[dict], date: str) -> list[dict]:
    """Joins ENTRY/EXIT by symbol. Strategy/side come straight from the
    entry's side -- no cross-file lookup against risk-decisions needed:
    SELL_TO_OPEN is always a short put in V1 (spec §6, no calls sold), and
    BUY_TO_OPEN's call-vs-put is read off the symbol itself. P&L formula
    hand-verified against the real 2026-08-31 log's known real outcome
    (+$22 TSLA, +$11 AAPL, -$3,960 SPY -- see PROGRESS.md)."""
    exits_by_symbol = {e["symbol"]: e for e in exits}
    trades = []
    for entry in entries:
        symbol = entry["symbol"]
        side_raw = entry["side"]
        if side_raw == "SELL_TO_OPEN":
            strategy, side = "premium-selling", "SHORT_PUT"
        elif side_raw == "BUY_TO_OPEN":
            strategy = "directional"
            side = "LONG_CALL" if option_type_from_symbol(symbol) == "C" else "LONG_PUT"
        else:
            raise ValueError(f"Unknown entry side: {side_raw!r} for {symbol}")

        trade = {
            "id": f"{date}-{symbol}", "date": date, "timestamp": entry["timestamp"],
            "ticker": entry["ticker"], "strategy": strategy, "side": side,
            "entry_price": entry["price"], "quantity": entry["quantity"],
        }

        exit_ = exits_by_symbol.get(symbol)
        if exit_ is None:
            trade.update(exit_price=None, pnl=None, status="OPEN", exit_reason=None)
        else:
            qty, entry_price, exit_price = entry["quantity"], entry["price"], exit_["price"]
            pnl = (entry_price - exit_price) if strategy == "premium-selling" else (exit_price - entry_price)
            trade.update(
                exit_price=exit_price, pnl=round(pnl * qty * 100, 2), status="CLOSED",
                exit_reason=EXIT_REASON_MAP.get(exit_["reason"], exit_["reason"]),
            )
        trades.append(trade)
    return trades


def build_daily_summary(trades: list[dict], date: str) -> dict:
    closed = [t for t in trades if t["status"] == "CLOSED"]
    wins = sum(1 for t in closed if t["pnl"] > 0)
    return {
        "date": date,
        "pnl": round(sum(t["pnl"] for t in closed), 2),
        "trades_count": len(trades),
        "win_rate": round(wins / len(closed), 4) if closed else 0.0,
    }


def build_strategy_stats(trades: list[dict]) -> list[dict]:
    by_strategy: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_strategy[t["strategy"]].append(t)

    stats = []
    for strategy, ts in by_strategy.items():
        closed = [t for t in ts if t["status"] == "CLOSED"]
        wins = sum(1 for t in closed if t["pnl"] > 0)
        losses = len(closed) - wins
        stats.append({
            "strategy": strategy,
            "trades": len(ts), "wins": wins, "losses": losses,
            "win_rate": round(wins / len(closed), 4) if closed else 0.0,
            "total_pnl": round(sum(t["pnl"] for t in closed), 2),
        })
    return stats


def discover_execution_logs() -> list[tuple[str, pathlib.Path]]:
    """Every logs/<date>-execution.log present, sorted oldest-first. Only
    one exists today (2026-08-31); scanning by filename rather than
    hardcoding a date means this grows on its own as more days accumulate."""
    found = []
    for path in LOGS_DIR.glob("*-execution.log"):
        m = _LOG_FILENAME_RE.match(path.name)
        if m:
            found.append((m.group(1), path))
    return sorted(found)


def main() -> None:
    all_trades: list[dict] = []
    daily_summaries: list[dict] = []
    for date, path in discover_execution_logs():
        entries, exits = parse_execution_log(path)
        trades = reconstruct_trades(entries, exits, date=date)
        all_trades.extend(trades)
        daily_summaries.append(build_daily_summary(trades, date=date))

    realized_pnl = sum(t["pnl"] for t in all_trades if t["status"] == "CLOSED")
    snapshot = risk_manager.get_account_snapshot()

    data = {
        "updated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "strategy_version": STRATEGY_VERSION,
        "account": {
            "starting_balance": STARTING_BALANCE,
            "balance": round(snapshot.equity, 2),
            "daily_pnl": round(realized_pnl, 2),
        },
        "daily_summaries": daily_summaries,
        "strategy_stats": build_strategy_stats(all_trades),
        "trades": sorted(all_trades, key=lambda t: t["timestamp"], reverse=True),
    }

    DASHBOARD_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DASHBOARD_DATA_PATH.write_text(json.dumps(data, indent=2) + "\n")
    print(f"Wrote {DASHBOARD_DATA_PATH} "
          f"({len(all_trades)} trades across {len(daily_summaries)} day(s)).")


if __name__ == "__main__":
    main()
