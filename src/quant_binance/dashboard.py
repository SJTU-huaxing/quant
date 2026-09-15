"""Loopback dashboard for public reports and virtual ledgers; no account imports."""

import json
import math
import re
import sqlite3
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
REPORTS = {
    "signals": "reports/tactical/signals.json",
    "paper": "reports/tactical/paper_status.json",
    "baseline": "reports/strategy_lab/latest.json",
    "factors": "reports/factor_lab/latest.json",
}


def safe_path(root, relative):
    """Internal allowlisted paths only. Refuse links before opening any file."""
    path = root / relative
    if path.resolve() != path or not path.is_relative_to(root):
        raise ValueError("Public path must remain in its declared location")
    return path


def public_json(root, relative):
    path = safe_path(root, relative)
    if path.stat().st_size > 16_000_000:
        raise ValueError("Public report too large")
    result = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError("Invalid public report")
    return result


def stamp(value):
    if isinstance(value, (int, float)) and math.isfinite(value):
        return value
    try:
        return datetime.fromisoformat(value).timestamp() * 1000
    except (TypeError, ValueError):
        return None


def freshness(value, now, limit):
    updated = stamp(value)
    age = (now - updated) / 1000 if updated is not None else None
    return dict(age_seconds=age, fresh=age is not None and -5 <= age <= limit)


def snapshot(root, now):
    result = dict(server_time=now, sources={}, errors=[])
    for name, relative in REPORTS.items():
        try:
            result[name] = public_json(root, relative)
        except (OSError, ValueError):
            result[name] = {}
            result["errors"].append(dict(source=name, reason="report-unavailable"))
    for name, field, limit in (
        ("signals", "created_utc", 180),
        ("paper", "updated_utc", 45),
        ("baseline", "updated_utc", 60),
        ("factors", "updated_utc", 1200),
    ):
        result["sources"][name] = freshness(result[name].get(field), now, limit)
    paper = result["paper"]
    experiment = paper.get("experiment", "")
    result["ledgers"] = {}
    if re.fullmatch(r"[0-9a-f]{20}", experiment):
        try:
            state = public_json(root, f"data/tactical/experiments/{experiment}/state.json")
            if state.get("experiment") != experiment:
                raise ValueError("Ledger/report mismatch")
            for lev, profile in state.get("profiles", {}).items():
                if lev not in ("1", "2", "3"):
                    continue
                book = profile["book"]
                # A writer may have committed state but not its matching report yet.
                if book["last_time"] > paper.get("updated_at", 0):
                    continue
                result["ledgers"][lev] = dict(
                    started=book["started"],
                    last_time=book["last_time"],
                    peak=book["peak"],
                    day_equity=book["day_equity"],
                    qty=book["qty"],
                    entry=book["entry"],
                    entry_time=book["entry_time"],
                    stop=book["stop"],
                    take_profit=profile.get("take_profit"),
                    mark=profile.get("last_mark"),
                    trades=book["trades"][-100:],
                    hourly_curve=book["equity_curve"][-720:],
                )
        except (OSError, ValueError, KeyError, TypeError):
            result["errors"].append(dict(source="ledger", reason="ledger-unavailable"))
    # Large research series are not needed for the dashboard table.
    for pair in result["baseline"].get("per_pair", []):
        pair.pop("holdout_curve", None)
    result["baseline"].get("portfolio", {}).pop("holdout_curve", None)
    return result


class History:
    """Independent display history, never modifies trading state or fabricates prices."""

    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS equity "
            "(experiment TEXT, leverage INTEGER, time INTEGER, equity REAL, "
            "PRIMARY KEY (experiment, leverage, time))"
        )

    def update(self, data):
        paper = data["paper"]
        experiment = paper.get("experiment", "")
        now = data["server_time"]
        for profile in paper.get("profiles", []):
            equity = profile.get("equity_usdt")
            if (
                data["sources"]["paper"]["fresh"]
                and profile.get("valuation_fresh")
                and isinstance(equity, (int, float))
                and math.isfinite(equity)
            ):
                self.db.execute(
                    "INSERT OR IGNORE INTO equity VALUES (?, ?, ?, ?)",
                    (experiment, profile["leverage"], paper["updated_at"], equity),
                )
        self.db.execute("DELETE FROM equity WHERE time < ?", (now - 30 * 86400000,))
        self.db.commit()
        data["curves"] = {}
        for lev in (1, 2, 3):
            rows = self.db.execute(
                "SELECT time, equity FROM equity WHERE experiment=? AND leverage=? ORDER BY time",
                (experiment, lev),
            ).fetchall()
            # Keep each bucket's extrema so the plot preserves visible drawdowns.
            if len(rows) > 1200:
                size = math.ceil(len(rows) / 400)
                reduced = []
                for offset in range(0, len(rows), size):
                    group = rows[offset : offset + size]
                    reduced.extend(
                        sorted(
                            set(
                                (
                                    group[0],
                                    min(group, key=lambda x: x[1]),
                                    max(group, key=lambda x: x[1]),
                                    group[-1],
                                )
                            )
                        )
                    )
                rows = reduced
            data["curves"][str(lev)] = rows

    def close(self):
        self.db.close()


def request_allowed(method, target, host, origin, port, site=""):
    """Pure routing validation: rejects unknown paths without filesystem access."""
    if method not in ("GET", "HEAD"):
        return 405
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    if host not in hosts or (origin and origin != "http://" + host) or site == "cross-site":
        return 403
    if target not in ASSETS and target not in ("/api/dashboard", "/api/health"):
        return 404
    return 200


class Dashboard:
    def __init__(self, root):
        self.root = root.resolve()
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.cache = b"{}"
        self.ready = False
        self.assets = {
            route: (safe_path(self.root, "web/" + filename).read_bytes(), mime)
            for route, (filename, mime) in ASSETS.items()
        }

    def collect(self):
        history = None
        try:
            history = History(safe_path(self.root, "data/dashboard/history.sqlite3"))
            while not self.stop.is_set():
                try:
                    data = snapshot(self.root, int(time.time() * 1000))
                    history.update(data)
                    payload = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
                    with self.lock:
                        self.cache, self.ready = payload, True
                except Exception as exc:
                    # No raw report contents or exception text in logs.
                    print(json.dumps(dict(dashboard_error=type(exc).__name__)), flush=True)
                self.stop.wait(5)
        finally:
            if history:
                history.close()

    def response(self, target):
        if target in self.assets:
            content, mime = self.assets[target]
            return 200, content, mime
        if target == "/api/health":
            return 200, b'{"service":"quant-public-dashboard","read_only":true}', "application/json"
        with self.lock:
            return (200 if self.ready else 503), self.cache, "application/json; charset=utf-8"


def serve(root: Path, port=8765):
    app = Dashboard(root)

    class Handler(BaseHTTPRequestHandler):
        server_version = "QuantLocal"

        def handle_request(self):
            status = request_allowed(
                self.command,
                self.path,
                self.headers.get("Host", ""),
                self.headers.get("Origin", ""),
                port,
                self.headers.get("Sec-Fetch-Site", ""),
            )
            body, mime = b'{"error":"request-not-allowed"}', "application/json"
            if status == 200:
                status, body, mime = app.response(self.path)
            self.send_response(status)
            for key, value in {
                "Content-Type": mime,
                "Content-Length": str(len(body)),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Cross-Origin-Resource-Policy": "same-origin",
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
                    "base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
                ),
                "Connection": "close",
            }.items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = handle_request

        def log_message(self, format, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        worker = threading.Thread(target=app.collect, daemon=True)
        worker.start()
        print(f"Quant public dashboard: http://127.0.0.1:{port}", flush=True)
        try:
            server.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            pass
        finally:
            app.stop.set()
            worker.join(timeout=10)
