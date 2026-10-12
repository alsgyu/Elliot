from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import secrets
import signal
import sys
import tempfile
import webbrowser
from dataclasses import asdict, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .analytics import availability, performance
from .config import ConfigError, RuntimeConfig, load_config, updated_grid, validate_grid_config
from .engine import TradingEngine
from .exchange import BinanceFuturesAdapter, PaperExchange
from .models import BotFlags, HaltReason
from .process_lock import ProcessLock
from .store import Store


class App:
    def __init__(self, config: RuntimeConfig):
        if config.mode.value == "live":
            raise ConfigError("live start blocked: account, protective-order and outage recovery validation is incomplete")
        self.config = config
        config.database_path = config.database_path.resolve()
        self.process_lock = ProcessLock(config.database_path.with_suffix(".lock"))
        self.account_lock = None
        if config.mode.value != "paper" and os.environ.get(config.api_key_env):
            identity_hash = hashlib.sha256((config.binance_base_url + config.symbol + os.environ[config.api_key_env]).encode()).hexdigest()
            try:
                self.account_lock = ProcessLock(Path(tempfile.gettempdir()) / f"kgridbot-{identity_hash}.lock")
            except Exception:
                self.process_lock.close()
                raise
        self.store = Store(config.database_path)
        identity = {"mode": config.mode.value, "symbol": config.symbol}
        prior = self.store.get_json("identity")
        if prior is not None and prior != identity:
            self.store.close()
            self.process_lock.close()
            if self.account_lock:
                self.account_lock.close()
            raise ConfigError("database mode/symbol mismatch: use a separate database")
        self.store.set_json("identity", identity)
        saved = self.store.get_json("grid_settings")
        if saved:
            config.grid = updated_grid(config.grid, saved, config.market_rules)
        self.exchange = self._make_exchange()
        self.engine = TradingEngine(config, self.store, self.exchange)
        if self.store.flags().can_enter:
            self.store.set_flags(BotFlags(HaltReason.USER_PAUSED, False, False, True))
        self.httpd = None
        self.csrf_token = secrets.token_urlsafe(32)
        self._closed = False
        self._engine_started = False

    def _make_exchange(self):
        if self.config.mode.value == "paper":
            initial = (self.config.grid.lower_price + self.config.grid.upper_price) / 2
            return PaperExchange(self.store, self.config.market_rules, initial,
                                 slippage_bps=self.config.grid.slippage_bps)
        return BinanceFuturesAdapter(self.store, self.config.market_rules, self.config.binance_base_url,
                                     self.config.api_key_env, self.config.api_secret_env, allow_trading=True)

    def start(self, open_browser: bool = False) -> None:
        if self.config.mode.value != "paper":
            errors = self.exchange.verify_account_prerequisites(self.config.grid.leverage)
            if errors:
                raise RuntimeError("exchange prerequisite check failed: " + "; ".join(errors))
            validate_grid_config(self.config.grid, self.config.market_rules)
        # Bind first: a port failure must never leave a trading thread running.
        self.httpd = ThreadingHTTPServer((self.config.host, self.config.port), self._make_handler())
        self.httpd.daemon_threads = True
        self.engine.start()
        self._engine_started = True
        url = f"http://127.0.0.1:{self.httpd.server_port}"
        print(f"KGridBot: {url} (mode={self.config.mode.value}, awaiting manual start)", flush=True)
        if open_browser:
            webbrowser.open(url)
        self.httpd.serve_forever()

    def shutdown(self) -> None:
        if self._closed:
            return
        self.engine.stop()
        try:
            if self._engine_started:
                self.engine.pause_entries()
        except Exception as exc:
            self.store.set_flags(BotFlags(HaltReason.RECONCILIATION_REQUIRED, False, True, True))
            self.store.add_event("ERROR", "shutdown cancellation was not confirmed", {"error": str(exc)})
            print("Shutdown: pending order cancellation requires reconciliation", file=sys.stderr)
        finally:
            if self.httpd:
                self.httpd.server_close()
            self.store.close()
            self.process_lock.close()
            if self.account_lock:
                self.account_lock.close()
            self._closed = True

    def _make_handler(self):
        app = self
        static_dir = Path(__file__).with_name("static").resolve()

        class Handler(BaseHTTPRequestHandler):
            server_version = "KGridBot/0.2"

            def _local_request(self):
                authority = self.headers.get("Host", "")
                allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
                return authority in allowed

            def do_GET(self):
                if not self._local_request():
                    self.send_error(403)
                    return
                path = urlsplit(self.path).path
                if path == "/" or path.startswith("/static/"):
                    target = (static_dir / ("index.html" if path == "/" else unquote(path[8:]))).resolve()
                    if not target.is_relative_to(static_dir) or not target.is_file():
                        self.send_error(404)
                        return
                    body = target.read_bytes()
                    self._body(body, mimetypes.guess_type(str(target))[0] or "application/octet-stream")
                    return
                if path == "/api/status":
                    self._json({**app.engine.status(), "csrf_token": app.csrf_token, "analytics": availability()})
                elif path == "/api/analytics":
                    with app.engine.lock:
                        samples = app.store.samples(200000)
                        capital = float(app.config.grid.allocated_margin)
                    self._json(performance(samples, capital))
                else:
                    self.send_error(404)

            def do_POST(self):
                origin = self.headers.get("Origin")
                if (not self._local_request() or self.headers.get("X-CSRF-Token") != app.csrf_token
                        or (origin and origin != "http://" + self.headers.get("Host", ""))):
                    self._json({"ok": False, "error": "local request token required"}, 403)
                    return
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    if not 0 <= length <= 16384:
                        raise ValueError("request is too large")
                    data = json.loads(self.rfile.read(length) or b"{}")
                    if not isinstance(data, dict):
                        raise ValueError("JSON object required")
                    actions = {"pause": app.engine.pause_entries, "resume": app.engine.resume_entries,
                               "emergency-stop": app.engine.emergency_stop,
                               "cancel-and-flatten": app.engine.cancel_entries_and_flatten}
                    path = urlsplit(self.path).path
                    if path.startswith("/api/control/") and path.rsplit("/", 1)[-1] in actions:
                        actions[path.rsplit("/", 1)[-1]]()
                        self._json({"ok": True, "status": app.engine.status()})
                    elif path == "/api/settings/preview":
                        self._json({"ok": True, "preview": app.engine.preview_settings(data)})
                    elif path == "/api/settings":
                        self._json({"ok": True, "preview": app.engine.update_settings(data)})
                    elif path == "/api/backtest":
                        from .backtest import run_backtest
                        with app.engine.lock:
                            config = replace(app.config, grid=updated_grid(app.config.grid, data, app.config.market_rules))
                        self._json({"ok": True, "report": run_backtest(config)})
                    else:
                        self.send_error(404)
                except (ValueError, ArithmeticError) as exc:
                    self._json({"ok": False, "error": str(exc)}, 400)
                except Exception as exc:
                    self._json({"ok": False, "error": str(exc)}, 409)

            def log_message(self, fmt, *args):
                pass

            def _body(self, body, content_type, status=200):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, payload, status=200):
                self._body(json.dumps(payload, ensure_ascii=False, default=_json_default, allow_nan=False).encode(),
                           "application/json; charset=utf-8", status)

        return Handler


def _json_default(value):
    if hasattr(value, "to_json"):
        return value.to_json()
    try:
        return asdict(value)
    except TypeError:
        return str(value)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Local BTC futures grid bot")
    parser.add_argument("--config", default="configs/paper.example.json")
    parser.add_argument("--port", type=int)
    parser.add_argument("--database")
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args(argv)
    app = None
    try:
        config = load_config(args.config)
        if args.port is not None:
            config.port = args.port
        if args.database:
            config.database_path = Path(args.database)
        app = App(config)
        def stop(_signum, _frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, stop)
        app.start(args.open)
    except KeyboardInterrupt:
        pass
    except (ValueError, OSError, RuntimeError, ArithmeticError) as exc:
        print(f"KGridBot: {exc}", file=sys.stderr)
        return 2
    finally:
        if app:
            app.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
