"""Public strategy lab: collect, research and continuously simulate on testnet. No dotenv."""

import argparse
import json
import logging
import os
import time
from dataclasses import asdict
from pathlib import Path

from quant_binance.errors import BinanceAPIError
from quant_binance.lab.runner import collect_history, run_research, snapshot_report
from quant_binance.lab.settings import LabSettings
from quant_binance.lab.store import Store

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("bootstrap", "research", "once", "watch"))
    parser.add_argument("--config", type=Path, default=ROOT / "strategy.toml")
    parser.add_argument("--data", type=Path, default=ROOT / "data/lab")
    parser.add_argument("--output", type=Path, default=ROOT / "reports/strategy_lab")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    settings = LabSettings.load(args.config)
    args.data.mkdir(parents=True, exist_ok=True)
    args.output.mkdir(parents=True, exist_ok=True)
    lock = args.data / "collector.lock"
    acquired = False
    try:
        if args.command == "watch":
            # Exclusive creation prevents two collectors from racing virtual trades.
            with lock.open("x", encoding="ascii") as handle:
                handle.write(str(os.getpid()))
            acquired = True
        with Store(args.data / "market.sqlite3") as store:
            if args.command == "bootstrap":
                results = {}
                for network, days in (("mainnet", settings.history_days), ("testnet", 20)):
                    results[network] = collect_history(store, settings, network, days=days)
                    print(
                        json.dumps({"network": network, "collected": results[network]}), flush=True
                    )
                return 0
            saved = args.output / "research.json"
            if args.command in ("once", "watch") and saved.exists():
                # Freeze the predeclared model for this forward experiment across restarts.
                research = json.loads(saved.read_text(encoding="utf-8"))
                if research.get("settings") != json.loads(json.dumps(asdict(settings))):
                    raise ValueError("Public settings changed; run research to start a new trial")
            else:
                collect_history(store, settings, "mainnet")
                research = run_research(store, settings, args.output)
            if args.command == "research":
                print(
                    json.dumps(
                        {
                            "historical_checks_passed": research["historical_checks_passed"],
                            "failures": research["historical_failures"],
                            "selected": {
                                k: research["selected"][k]
                                for k in ("symbol", "strategy", "holdout", "stress")
                            },
                        },
                        indent=2,
                    )
                )
                return 0
            last_refresh, failures = 0, 0
            while not (args.data / "STOP").exists():
                started = time.monotonic()
                try:
                    if time.monotonic() - last_refresh > 300:
                        for network, days in (("mainnet", settings.history_days), ("testnet", 20)):
                            collect_history(store, settings, network, days=days)
                        last_refresh = time.monotonic()
                    report = snapshot_report(store, settings, args.output, research)
                    print(
                        json.dumps(
                            {
                                "updated_utc": report["updated_utc"],
                                "public_snapshots": len(report["snapshots"]),
                                "collection_errors": report["collection_errors"],
                                "exchange_orders_sent": 0,
                                "mainnet_enabled": False,
                            }
                        ),
                        flush=True,
                    )
                    failures = 1 if report["collection_errors"] else 0
                except BinanceAPIError as exc:
                    if exc.status in (418, 429):
                        print(
                            json.dumps(
                                {"stopped": "exchange-rate-limit", "http_status": exc.status}
                            ),
                            flush=True,
                        )
                        return 1
                    failures += 1
                except Exception as exc:
                    failures += 1
                    print(json.dumps({"collection_failed": type(exc).__name__}), flush=True)
                if args.command == "once":
                    return 1 if failures else 0
                # No trading while disconnected; quote age/gaps gate paper entries on resume.
                delay = min(60, settings.poll_seconds * max(1, failures))
                time.sleep(max(1, delay - (time.monotonic() - started)))
            print("Collector stopped by local STOP file. No exchange orders were sent.")
            return 0
    except FileExistsError:
        print("Collector lock exists. Check its process before starting another collector.")
        return 1
    except (KeyboardInterrupt, EOFError):
        print("Collector stopped. Virtual account state is saved; no exchange orders were sent.")
        return 130
    except Exception as exc:
        print(
            json.dumps(
                {
                    "failed": type(exc).__name__,
                    "details": "See public-data configuration and tests.",
                }
            )
        )
        return 1
    finally:
        if acquired:
            lock.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
