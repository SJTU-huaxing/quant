"""Screen public factor/strategy experiments once; no credentials or execution path."""

import argparse
import json
import os
import time
from pathlib import Path

from quant_binance.lab.factor_lab import run

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="Use only cached public outcomes")
    args = parser.parse_args()
    lock = ROOT / "data/factor_lab/research.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    acquired = False
    try:
        with lock.open("x", encoding="ascii") as f:
            f.write(str(os.getpid()))
        acquired = True
        r = run(ROOT, int(time.time() * 1000), online=not args.offline)
        print(
            json.dumps(
                {
                    k: r[k]
                    for k in (
                        "updated_utc",
                        "version",
                        "recorded_events",
                        "matured_events",
                        "pending_events",
                        "tested_definitions",
                        "execution_policy_changed",
                        "errors",
                    )
                }
            )
        )
        return 0
    except FileExistsError:
        print("Research lock exists; do not start a duplicate research run.")
        return 1
    except Exception as exc:
        print(json.dumps(dict(research_failed=type(exc).__name__)))
        return 1
    finally:
        if acquired:
            lock.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
