"""Collect public testnet data and replay simulated fills; never loads credentials."""

import argparse
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from quant_binance.errors import QuantError
from quant_binance.testnet_market import TestnetMarket, paper_trade


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", choices=("BTCUSDT", "ETHUSDT"), default="BTCUSDT")
    parser.add_argument("--limit", type=int, default=500)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        with TestnetMarket() as market:
            snapshot = market.snapshot(args.symbol, args.limit)
        paper = paper_trade(snapshot)
        root = Path(__file__).resolve().parents[1]
        destination = root / "data" / "testnet"
        destination.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8]
        path = destination / f"{args.symbol}_{stamp}.json"
        with path.open("x", encoding="utf-8") as handle:
            json.dump({"market": snapshot, "paper": paper}, handle, indent=2)
        print(
            json.dumps(
                {
                    "connection": "direct",
                    "network": "testnet",
                    "symbol": args.symbol,
                    "completed_candles": len(snapshot["candles"]),
                    "simulated_fills": len(paper["fills"]),
                    "exchange_orders_sent": 0,
                    "output": str(path),
                },
                indent=2,
            )
        )
        return 0
    except QuantError as exc:
        print(f"Error: {exc}")
        return 1
    except Exception:
        print("Collection or replay failed; details suppressed.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
