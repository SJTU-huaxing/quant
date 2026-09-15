"""Serve the public quant dashboard on localhost; never loads account configuration."""

import argparse
from pathlib import Path

from quant_binance.dashboard import serve


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Use a local port between 1024 and 65535")
    serve(Path(__file__).resolve().parents[1], args.port)


if __name__ == "__main__":
    main()
