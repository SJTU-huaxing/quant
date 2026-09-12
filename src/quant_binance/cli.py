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
    result.add_argument("--network", choices=("testnet", "mainnet"), help="Select Binance network")
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
        configured = Settings.load(args.env_file)
        private_command = args.command in ("account", "permissions")
        target_market = args.market or configured.market
        target_network = args.network or configured.network
        if (
            private_command
            and not args.prompt_credentials
            and (configured.api_key or configured.api_secret)
            and (target_market, target_network) != (configured.market, configured.network)
        ):
            raise ConfigurationError(
                "Credential market/network mismatch. Use a matching local env file "
                "or --prompt-credentials for the selected destination."
            )
        settings = replace(configured, market=target_market, network=target_network)
        if not private_command or args.prompt_credentials:
            settings = replace(settings, api_key="", api_secret="")
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

        output = {"market": settings.market, "network": settings.network}
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
