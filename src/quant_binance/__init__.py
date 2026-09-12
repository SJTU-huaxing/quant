"""Read-only Binance Futures and Spot connectivity. No trading endpoints are implemented."""

from .client import BinanceClient
from .config import Settings
from .errors import BinanceAPIError, QuantError

__all__ = ["BinanceAPIError", "BinanceClient", "QuantError", "Settings"]
