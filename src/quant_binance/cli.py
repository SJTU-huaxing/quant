"""CLI never accepts credentials as command-line arguments or emits account IDs."""

import argparse
import getpass
import json
import logging
import sys
import warnings
from dataclasses import replace
from pathlib import Path

from .client import BinanceClient
from .config import Settings
from .errors import ConfigurationError, QuantError
from .privacy import account_summary, permission_summary


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Read-only Binance Futures/Spot API connection tools"
    )
    result.add_argument("--env-file", type=Path, help="Explicit local .env path")
    result.add_argument(
        "--network",
        choices=("testnet", "mainnet"),
        help="Select network and matching MAIN/TEST keys; account commands prompt if omitted",
    )
    result.add_argument(
        "--market", choices=("usdm", "spot"), help="USD-M Futures (default) or Spot"
    )
    result.add_argument(
        "--prompt-credentials", action="store_true", help="Read HMAC credentials with hidden input"
    )
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("ping", help="Check public connectivity and server time (no keys required)")
    price = commands.add_parser("price", help="Read a public symbol price")
    price.add_argument("--symbol", default="BTCUSDT")
    account = commands.add_parser("account", help="Verify signed read-only account access")
    account.add_argument("--show-balances", action="store_true", help="Opt in to private balances")
    commands.add_parser("permissions", help="Inspect mainnet API key permission flags")
    return result


def main(argv: list[str] | None = None) -> int:
    # httpx INFO logging contains full URLs, including signed query strings.
    for name in ("httpx", "httpcore", "dotenv.main"):
        logging.getLogger(name).disabled = True
    args = parser().parse_args(argv)
    previous_log_threshold = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        private_command = args.command in ("account", "permissions")
        target_network = args.network
        if private_command and target_network is None:
            if not sys.stdin.isatty():
                raise ConfigurationError(
                    "Select --network mainnet or testnet, or run scripts/verify_accounts.py "
                    "to check both accounts in your own terminal."
                )
            choice = input("Network: 1 = testnet, 2 = mainnet: ").strip().lower()
            target_network = {"1": "testnet", "2": "mainnet"}.get(choice, choice)
            if target_network not in ("testnet", "mainnet"):
                raise ConfigurationError("Select testnet or mainnet.")
        target_network = target_network or "testnet"
        if private_command and not args.prompt_credentials:
            settings = Settings.load(args.env_file, network=target_network, market=args.market)
        else:
            # Public requests and hidden-input mode never need a dotenv file.
            settings = Settings(market=args.market or "usdm", network=target_network)
        if args.prompt_credentials:
            if not private_command:
                raise ConfigurationError("Credential prompting is only for account/permissions.")
            if not sys.stdin.isatty() or not sys.stderr.isatty():
                raise ConfigurationError(
                    "Hidden credential entry requires your own interactive terminal."
                )
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                try:
                    settings = replace(
                        settings,
                        api_key=getpass.getpass("Binance HMAC API key (hidden): ").strip(),
                        api_secret=getpass.getpass("Binance HMAC secret (hidden): ").strip(),
                    )
                except getpass.GetPassWarning:
                    raise ConfigurationError(
                        "Hidden input unavailable; credentials were not read."
                    ) from None
        if private_command:
            settings.require_credentials()

        output = {
            "market": settings.market,
            "network": settings.network,
            "connection_mode": "system-route",
        }
        with BinanceClient(settings) as client:
            if args.command == "ping":
                client.ping()
                output.update({"connected": True, "server_time_ms": client.sync_time()})
            elif args.command == "price":
                price = client.price(args.symbol)
                output.update({"symbol": price.get("symbol"), "price": price.get("price")})
            elif args.command == "account":
                output.update(
                    account_summary(
                        client.account(), market=settings.market, show_balances=args.show_balances
                    )
                )
            else:
                output.update(permission_summary(client.key_permissions()))
        print(json.dumps(output, ensure_ascii=True, indent=2))
        return 0
    except QuantError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("Cancelled.", file=sys.stderr)
        return 130
    except Exception:
        # No traceback with request locals or private payloads in the user-facing CLI.
        print(
            "Unexpected failure; private details suppressed. Run the offline tests.",
            file=sys.stderr,
        )
        return 1
    finally:
        logging.disable(previous_log_threshold)
