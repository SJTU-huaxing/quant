"""Allowlist CLI output instead of attempting to redact an arbitrary API payload."""

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from .errors import QuantError


def account_summary(
    payload: dict[str, Any], *, market: str = "spot", show_balances: bool = False
) -> dict[str, Any]:
    if market == "usdm":
        return futures_account_summary(payload, show_balances=show_balances)
    if not isinstance(payload.get("balances"), list) or type(payload.get("canTrade")) is not bool:
        raise QuantError("Unexpected account response; account verification is incomplete.")
    result: dict[str, Any] = {
        "authenticated": True,
        "read_only_client": True,
        "balances_hidden": not show_balances,
    }
    if show_balances:
        balances = []
        for row in payload["balances"]:
            if not isinstance(row, dict):
                raise QuantError("Unexpected balance response; details suppressed.")
            asset, free, locked = row.get("asset"), row.get("free"), row.get("locked")
            if not isinstance(asset, str) or not re.fullmatch(r"\w{1,30}", asset):
                raise QuantError("Unexpected asset response; details suppressed.")
            try:
                if not isinstance(free, str) or not isinstance(locked, str):
                    raise ValueError
                amounts = (Decimal(free), Decimal(locked))
                if any(not value.is_finite() or value < 0 for value in amounts):
                    raise ValueError
            except (InvalidOperation, ValueError):
                raise QuantError("Unexpected balance value; details suppressed.") from None
            if any(value != 0 for value in amounts):
                balances.append(
                    {"asset": asset, "free": str(amounts[0]), "locked": str(amounts[1])}
                )
        result["balances"] = balances
    return result


def futures_account_summary(
    payload: dict[str, Any], *, show_balances: bool = False
) -> dict[str, Any]:
    if not isinstance(payload.get("assets"), list) or not isinstance(
        payload.get("positions"), list
    ):
        raise QuantError("Unexpected futures account response; verification is incomplete.")
    result: dict[str, Any] = {
        "authenticated": True,
        "read_only_client": True,
        "balances_hidden": not show_balances,
        "positions_hidden": True,
    }
    if show_balances:
        balances = []
        for row in payload["assets"]:
            if not isinstance(row, dict) or not isinstance(row.get("asset"), str):
                raise QuantError("Unexpected futures asset response; details suppressed.")
            asset = row["asset"]
            if not re.fullmatch(r"\w{1,30}", asset):
                raise QuantError("Unexpected futures asset response; details suppressed.")
            fields = ("walletBalance", "availableBalance", "unrealizedProfit")
            try:
                amounts = {
                    name: Decimal(row[name]) for name in fields if isinstance(row[name], str)
                }
                if len(amounts) != len(fields) or any(not n.is_finite() for n in amounts.values()):
                    raise ValueError
            except (KeyError, InvalidOperation, ValueError):
                raise QuantError("Unexpected futures balance value; details suppressed.") from None
            if any(n != 0 for n in amounts.values()):
                balances.append({"asset": asset, **{k: str(v) for k, v in amounts.items()}})
        result["balances"] = balances
    return result


def permission_summary(payload: dict[str, Any]) -> dict[str, Any]:
    names = (
        "ipRestrict",
        "enableReading",
        "enableSpotAndMarginTrading",
        "enableWithdrawals",
        "enableInternalTransfer",
        "enableFutures",
        "permitsUniversalTransfer",
        "enableMargin",
        "enablePortfolioMarginTrading",
    )
    flags = {name: payload[name] for name in names if type(payload.get(name)) is bool}
    if "enableReading" not in flags:
        raise QuantError("Unexpected API permission response; details suppressed.")
    return {"key_permissions": flags}
