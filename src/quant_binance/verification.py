"""User-run, read-only account verification with an explicit network selection."""

import logging
from pathlib import Path
from typing import Any

from .client import BinanceClient
from .config import Settings
from .errors import BinanceAPIError, QuantError
from .privacy import account_summary


def verify_account(
    network: str, *, market: str = "usdm", env_file: Path | None = None
) -> dict[str, Any]:
    """Check public connectivity and the selected key pair; return no private data.

    This function loads local credentials. Users must run it in their own terminal;
    agents must use the independent public network probe instead.
    """
    public_settings = Settings(network=network, market=market)
    result: dict[str, Any] = {
        "market": market,
        "network": network,
        "connection_mode": "system-route",
        "public_connected": False,
        "authentication_attempted": False,
        "authenticated": False,
        "read_only_client": True,
        "balances_hidden": True,
        "positions_hidden": True,
    }
    previous_log_threshold = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    stage = "public-connectivity"
    try:
        with BinanceClient(public_settings) as client:
            client.ping()
            client.sync_time()
        result["public_connected"] = True
        stage = "local-configuration"
        settings = Settings.load(env_file, network=network, market=market)
        settings.require_credentials()
        stage = "account-authentication"
        result["authentication_attempted"] = True
        with BinanceClient(settings) as client:
            result.update(account_summary(client.account(), market=market))
    except BinanceAPIError as exc:
        result.update(error=str(exc), failure_stage=stage, http_status=exc.status)
        if exc.code is not None:
            result["api_code"] = exc.code
    except QuantError as exc:
        result.update(error=str(exc), failure_stage=stage)
    except Exception:
        result.update(error="Verification failed; private details suppressed.", failure_stage=stage)
    finally:
        logging.disable(previous_log_threshold)
    return result
