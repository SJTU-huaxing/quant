"""User-run testnet authentication and one bounded round trip. Agents must not run this CLI."""

import argparse
import hashlib
import hmac
import json
import logging
import time
from decimal import Decimal
from urllib.parse import urlencode
from uuid import uuid4

import httpx

from .client import BinanceClient
from .config import Settings
from .errors import BinanceAPIError, ConfigurationError, QuantError
from .privacy import account_summary
from .testnet_market import MAX_NOTIONAL, TESTNET_URL, TestnetMarket, market_quantity, number

GET_PATHS = {"/fapi/v1/positionSide/dual", "/fapi/v1/openOrders", "/fapi/v1/order"}
TERMINAL = {"FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"}


class TestnetExecutor:
    __test__ = False

    def __init__(self, settings, *, transport=None, sleep=time.sleep):
        if (settings.market, settings.network, settings.base_url) != (
            "usdm",
            "testnet",
            TESTNET_URL,
        ):
            raise ConfigurationError("Execution is limited to the fixed USD-M testnet endpoint.")
        settings.require_credentials()
        self.read = BinanceClient(settings, transport=transport)
        self.settings = settings
        self.sleep = sleep

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.read.close()

    def authenticate(self):
        payload = self.read.account()
        return account_summary(payload, market="usdm")

    def _signed(self, method, path, params):
        if not (
            (method == "GET" and path in GET_PATHS)
            or (method == "POST" and path == "/fapi/v1/order")
        ):
            raise ConfigurationError("Execution endpoint is not allowed.")
        if (self.settings.market, self.settings.network, self.settings.base_url) != (
            "usdm",
            "testnet",
            TESTNET_URL,
        ):
            raise ConfigurationError("Execution destination changed; request blocked.")
        if (
            self.read._monotonic_anchor is None
            or self.read._clock() - self.read._monotonic_anchor > 30
        ):
            self.read.sync_time()
        query = dict(params)
        query["recvWindow"] = self.settings.recv_window_ms
        query["timestamp"] = int(
            self.read._server_anchor_ms + (self.read._clock() - self.read._monotonic_anchor) * 1000
        )
        encoded = urlencode(query)
        signature = hmac.new(
            self.settings.api_secret.encode("ascii"), encoded.encode("ascii"), hashlib.sha256
        ).hexdigest()
        signed = encoded + "&signature=" + signature
        headers = {"X-MBX-APIKEY": self.settings.api_key}
        if method == "GET":
            url, content = TESTNET_URL + path + "?" + signed, None
        else:
            url, content = TESTNET_URL + path, signed.encode("ascii")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            response = self.read._http.request(method, url, content=content, headers=headers)
        except httpx.HTTPError:
            # A POST may have reached the exchange. The caller queries its client order ID.
            raise QuantError("Testnet request failed; order state may be unknown.") from None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if response.status_code != 200:
            code = payload.get("code") if isinstance(payload, dict) else None
            raise BinanceAPIError(response.status_code, code if type(code) is int else None)
        if not isinstance(payload, (dict, list)):
            raise QuantError("Invalid testnet response; order state may be unknown.")
        return payload

    def _query_order(self, symbol, client_id):
        result = self._signed(
            "GET", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": client_id}
        )
        if not isinstance(result, dict):
            raise QuantError("Invalid order status response.")
        return result

    def _market_order(self, symbol, side, quantity, *, reduce_only):
        client_id = "quant-smoke-" + uuid4().hex[:24]
        params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": format(quantity, "f"),
            "newClientOrderId": client_id,
            "newOrderRespType": "RESULT",
        }
        if reduce_only:
            params["reduceOnly"] = "true"

        def executed_quantity(result):
            if (result.get("symbol"), result.get("clientOrderId"), result.get("side")) != (
                symbol,
                client_id,
                side,
            ):
                raise QuantError("Order identity mismatch; inspect the testnet account.")
            executed = number(result.get("executedQty"), zero=True)
            if executed > quantity:
                raise QuantError("Unexpected executed quantity; inspect the testnet account.")
            return executed

        try:
            order = self._signed("POST", "/fapi/v1/order", params)
        except BinanceAPIError as exc:
            if exc.status < 500:
                raise  # No retry of rejected orders, especially rate limits.
            order = None
        except QuantError:
            order = None
        # Never resubmit a POST, including after timeouts or 503 unknown-execution responses.
        for attempt in range(4):
            if isinstance(order, dict) and order.get("status") in TERMINAL:
                return executed_quantity(order)
            if attempt:
                self.sleep(1)
            try:
                order = self._query_order(symbol, client_id)
            except BinanceAPIError as exc:
                if exc.status in (418, 429) or (exc.status < 500 and exc.code != -2013):
                    raise
                order = None
            except QuantError:
                order = None
        if isinstance(order, dict) and order.get("status") in TERMINAL:
            return executed_quantity(order)
        raise QuantError(
            "Order state unresolved; no duplicate order sent. Inspect the testnet account."
        )

    @staticmethod
    def _positions(payload):
        account_summary(payload, market="usdm")
        positions = payload["positions"]
        try:
            amounts = [Decimal(str(item["positionAmt"])) for item in positions]
        except (KeyError, TypeError, ValueError, ArithmeticError):
            raise QuantError("Invalid position information.") from None
        if any(not amount.is_finite() for amount in amounts):
            raise QuantError("Invalid position amount.")
        return amounts

    def round_trip(self, snapshot):
        symbol = snapshot["symbol"]
        if symbol not in ("BTCUSDT", "ETHUSDT") or snapshot.get("source") != TESTNET_URL:
            raise ConfigurationError("Only collected BTCUSDT/ETHUSDT testnet data is supported.")
        account = self.read.account()
        if any(self._positions(account)):
            raise QuantError("Existing testnet positions found; no order sent.")
        if account.get("canTrade") is False:
            raise QuantError("Testnet account cannot trade; no order sent.")
        mode = self._signed("GET", "/fapi/v1/positionSide/dual", {})
        if not isinstance(mode, dict) or mode.get("dualSidePosition") is not False:
            raise QuantError(
                "This test requires one-way position mode; no account setting changed."
            )
        orders = self._signed("GET", "/fapi/v1/openOrders", {})
        if not isinstance(orders, list) or orders:
            raise QuantError("Open orders found or unavailable; no order sent.")
        # Refresh rules and the executable quote immediately before sizing the order.
        with TestnetMarket() as market:
            latest = market.snapshot(symbol, 60)
        ask, bid = number(latest["ask"]), number(latest["bid"])
        if (ask - bid) / ask > Decimal("0.005"):
            raise QuantError("Testnet spread exceeds 0.5%; no order sent.")
        quantity = market_quantity(latest["filters"], ask)
        entered = self._market_order(symbol, "BUY", quantity, reduce_only=False)
        if entered == 0:
            raise QuantError("Entry did not execute; no closing order was needed.")
        try:
            closed = self._market_order(symbol, "SELL", entered, reduce_only=True)
            remaining = self._positions(self.read.account())
        except QuantError:
            raise QuantError(
                "Entry executed but closure is unconfirmed. Inspect the testnet account."
            ) from None
        if closed != entered or any(remaining):
            raise QuantError("Testnet position is not confirmed flat; inspect it before rerunning.")
        return {
            "authenticated": True,
            "network": "testnet",
            "connection": "direct",
            "round_trip": "completed",
            "flat_after": True,
            "sizing_notional_limit_usdt": str(MAX_NOTIONAL),
            "account_details_hidden": True,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--round-trip",
        action="store_true",
        help="Place one minimum-sized testnet BUY and a reduce-only SELL",
    )
    parser.add_argument("--symbol", choices=("BTCUSDT", "ETHUSDT"), default="BTCUSDT")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        # User terminal only. Never run this entry point as an agent.
        settings = Settings.load()
        with TestnetExecutor(settings) as executor:
            if args.round_trip:
                with TestnetMarket() as market:
                    snapshot = market.snapshot(args.symbol, 60)
                output = executor.round_trip(snapshot)
            else:
                output = executor.authenticate()
                output.update(network="testnet", connection="direct")
        print(json.dumps(output, indent=2))
        return 0
    except QuantError as exc:
        print(f"Error: {exc}")
        return 1
    except (KeyboardInterrupt, EOFError):
        print("Interrupted. Inspect the testnet account if a trade was in progress.")
        return 130
    except Exception:
        print("Verification failed; inspect the testnet account if a trade was in progress.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
