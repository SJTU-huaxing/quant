"""User terminal only: verify both accounts without printing keys, balances or positions."""

import argparse
import json
from pathlib import Path

from quant_binance.verification import verify_account

# Script users can select one network here, without command-line flags.
NETWORKS = ("mainnet", "testnet")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", choices=("mainnet", "testnet", "both"))
    parser.add_argument("--market", choices=("usdm", "spot"), default="usdm")
    parser.add_argument(
        "--env-file", type=Path, default=Path(__file__).resolve().parents[1] / ".env"
    )
    args = parser.parse_args(argv)
    networks = (
        NETWORKS
        if args.network is None
        else (("mainnet", "testnet") if args.network == "both" else (args.network,))
    )
    results = []
    try:
        for network in networks:
            result = verify_account(network, market=args.market, env_file=args.env_file)
            results.append(result)
            if result.get("http_status") in (418, 429):
                break
    except (KeyboardInterrupt, EOFError):
        print("Cancelled. No orders were sent.")
        return 130
    print(json.dumps(results, ensure_ascii=True, indent=2))
    return 0 if len(results) == len(networks) and all(r["authenticated"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
