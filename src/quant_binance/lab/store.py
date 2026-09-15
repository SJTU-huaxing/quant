"""SQLite stores public market data and virtual accounts only; never exchange accounts."""

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from .engine import Book
from .market import Bar, Rules


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS bars (
                network TEXT, symbol TEXT, interval TEXT, time INTEGER, payload TEXT,
                PRIMARY KEY(network,symbol,interval,time));
            CREATE TABLE IF NOT EXISTS funding (
                network TEXT, symbol TEXT, time INTEGER, payload TEXT,
                PRIMARY KEY(network,symbol,time));
            CREATE TABLE IF NOT EXISTS quotes (
                network TEXT, symbol TEXT, time INTEGER, payload TEXT,
                PRIMARY KEY(network,symbol,time));
            CREATE TABLE IF NOT EXISTS rules (
                network TEXT, symbol TEXT, payload TEXT, PRIMARY KEY(network,symbol));
            CREATE TABLE IF NOT EXISTS paper (
                experiment TEXT, symbol TEXT, strategy TEXT, payload TEXT,
                PRIMARY KEY(experiment,symbol,strategy));
        """)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    def put_bars(self, network, symbol, interval, bars):
        with self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?)",
                [(network, symbol, interval, b.time, json.dumps(asdict(b))) for b in bars],
            )

    def bars(self, network, symbol, interval):
        return [
            Bar(**json.loads(r[0]))
            for r in self.db.execute(
                "SELECT payload FROM bars WHERE network=? AND symbol=? AND interval=? "
                "ORDER BY time",
                (network, symbol, interval),
            )
        ]

    def put_events(self, table, network, symbol, events):
        if table not in ("funding", "quotes"):
            raise ValueError("Public event table required")
        with self.db:
            self.db.executemany(
                f"INSERT OR REPLACE INTO {table} VALUES (?,?,?,?)",
                [(network, symbol, r["time"], json.dumps(r)) for r in events],
            )

    def events(self, table, network, symbol, *, start=0):
        if table not in ("funding", "quotes"):
            raise ValueError("Public event table required")
        return [
            json.loads(r[0])
            for r in self.db.execute(
                f"SELECT payload FROM {table} WHERE network=? AND symbol=? AND time>=? "
                "ORDER BY time",
                (network, symbol, start),
            )
        ]

    def put_rules(self, network, rules):
        with self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO rules VALUES (?,?,?)",
                [(network, s, json.dumps(asdict(r))) for s, r in rules.items()],
            )

    def rules(self, network, symbol):
        row = self.db.execute(
            "SELECT payload FROM rules WHERE network=? AND symbol=?", (network, symbol)
        ).fetchone()
        if row is None:
            raise ValueError("Collect exchange filters first")
        return Rules(**json.loads(row[0]))

    def paper(self, experiment, symbol, strategy):
        row = self.db.execute(
            "SELECT payload FROM paper WHERE experiment=? AND symbol=? AND strategy=?",
            (experiment, symbol, strategy),
        ).fetchone()
        return Book(**json.loads(row[0])) if row else None

    def put_paper(self, experiment, symbol, strategy, book):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO paper VALUES (?,?,?,?)",
                (experiment, symbol, strategy, json.dumps(asdict(book))),
            )
