"""Public short-term scanner and explicit-review paper trials. Never loads credentials."""

import argparse
import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from quant_binance.errors import BinanceAPIError
from quant_binance.lab.runner import atomic_json
from quant_binance.lab.store import Store
from quant_binance.lab.tactical_engine import ticket
from quant_binance.lab.tactical_market import scan
from quant_binance.lab.tactical_migration import migrate_flat_state
from quant_binance.lab.tactical_runner import paper_tick
from quant_binance.lab.tactical_settings import TacticalSettings

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("scan", "scan-watch", "paper-once", "paper-watch", "review", "status", "migrate"),
    )
    parser.add_argument("--config", type=Path, default=ROOT / "tactical.toml")
    parser.add_argument("--data", type=Path, default=ROOT / "data/tactical")
    parser.add_argument("--output", type=Path, default=ROOT / "reports/tactical")
    parser.add_argument("--snapshot-id")
    parser.add_argument("--candidate")
    parser.add_argument("--rationale")
    parser.add_argument("--previous-experiment")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    settings = TacticalSettings.load(args.config)
    args.data.mkdir(parents=True, exist_ok=True)
    args.output.mkdir(parents=True, exist_ok=True)
    if args.command == "migrate":
        result = migrate_flat_state(
            args.data,
            settings,
            args.previous_experiment,
            int(datetime.now(UTC).timestamp() * 1000),
        )
        print(json.dumps(result))
        return 0
    if args.command == "review":
        snapshot = json.loads((args.output / "signals.json").read_text(encoding="utf-8"))
        if not args.snapshot_id or args.snapshot_id != snapshot["snapshot_id"]:
            parser.error("Use the exact snapshot identifier you reviewed; the latest scan changed.")
        if not args.candidate or not args.rationale:
            parser.error("Provide candidate ID (or hold) and an explicit rationale.")
        now = int(datetime.now(UTC).timestamp() * 1000)
        decision = ticket(snapshot, args.candidate, args.rationale, settings, now)
        atomic_json(args.output / "decisions" / (decision["decision_id"] + ".json"), decision)
        atomic_json(args.output / "decision.json", decision)
        print(json.dumps(decision, ensure_ascii=True, indent=2))
        return 0
    if args.command == "status":
        for name in ("signals.json", "paper_status.json"):
            path = args.output / name
            if path.exists():
                r = json.loads(path.read_text(encoding="utf-8"))
                keys = (
                    ("created_utc", "snapshot_id", "ranking", "errors")
                    if name == "signals.json"
                    else (
                        "updated_utc",
                        "profiles",
                        "decisions",
                        "errors",
                        "exchange_orders_sent",
                        "mainnet_enabled",
                    )
                )
                print(json.dumps({k: r[k] for k in keys}, ensure_ascii=True, indent=2))
        return 0
    scan_mode = args.command.startswith("scan")
    watch = args.command.endswith("watch")
    lock = args.data / ("scan.lock" if scan_mode else "paper.lock")
    acquired = False
    try:
        # One-shot mutations also respect locks held by ongoing workers.
        with lock.open("x", encoding="ascii") as handle:
            handle.write(str(os.getpid()))
        acquired = True
        failures = 0
        with Store(args.data / "market.sqlite3") as store:
            while not (args.data / "STOP").exists():
                started = time.monotonic()
                try:
                    if scan_mode:
                        report = scan(store, settings, args.output)
                        result = dict(
                            created_utc=report["created_utc"],
                            snapshot_id=report["snapshot_id"],
                            symbols=len(report["rows"]),
                            eligible=sum(c["eligible"] for c in report["ranking"]),
                            errors=report["errors"],
                            exchange_orders_sent=0,
                        )
                    else:
                        report = paper_tick(store, settings, args.data, args.output)
                        result = dict(
                            updated_utc=report["updated_utc"],
                            profiles=[
                                dict(
                                    leverage=p["leverage"],
                                    status=p["status"],
                                    equity_usdt=p["equity_usdt"],
                                )
                                for p in report["profiles"]
                            ],
                            decisions=len(report["decisions"]),
                            errors=report["errors"],
                            exchange_orders_sent=0,
                        )
                    print(json.dumps(result, ensure_ascii=True), flush=True)
                    failures = 1 if report["errors"] else 0
                except BinanceAPIError as exc:
                    if exc.status in (418, 429):
                        print(
                            json.dumps(dict(stopped="exchange-rate-limit", http_status=exc.status)),
                            flush=True,
                        )
                        return 1
                    failures += 1
                    print(
                        json.dumps(dict(error_type=type(exc).__name__, http_status=exc.status)),
                        flush=True,
                    )
                except Exception as exc:
                    failures += 1
                    print(json.dumps(dict(error_type=type(exc).__name__)), flush=True)
                if not watch:
                    return 1 if failures else 0
                cadence = settings.scan_seconds if scan_mode else settings.poll_seconds
                delay = min(300 if scan_mode else 60, cadence * max(1, failures))
                wake = started + delay
                while time.monotonic() < wake and not (args.data / "STOP").exists():
                    time.sleep(min(5, max(0.01, wake - time.monotonic())))
        return 0
    except FileExistsError:
        print("Worker lock exists; do not start a duplicate scanner or paper worker.")
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        if acquired:
            lock.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
