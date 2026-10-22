from __future__ import annotations

import argparse
import json
import mimetypes
from pathlib import Path
import secrets
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit
import webbrowser

from .journal import dumps
from .chart import ChartReader
from .service import Service


def handler(service):
    charts = ChartReader(service)
    token = secrets.token_urlsafe(32)
    static = Path(__file__).with_name("static").resolve()
    vendor = Path(__file__).parent.parent / "kgridbot" / "static" / "vendor"
    job = {"running": False, "report": None, "error": None}
    job_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def local(self):
            return self.headers.get("Host") in {f"localhost:{self.server.server_port}", f"127.0.0.1:{self.server.server_port}"}

        def send(self, data, status=200, content="application/json; charset=utf-8", filename=None):
            body = data if isinstance(data, bytes) else dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", content)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; form-action 'self'")
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.local():
                self.send({"error": "loopback host required"}, 403)
                return
            path = urlsplit(self.path).path
            try:
                if path == "/api/status":
                    self.send({**service.status(), "csrf": token})
                elif path == "/api/chart":
                    query = parse_qs(urlsplit(self.path).query)
                    self.send(charts.get(query.get("timeframe", ["4h"])[0]))
                elif path == "/api/backtest":
                    with job_lock:
                        self.send(job)
                elif path.startswith("/journal/") and path[9:].isdigit():
                    with service.lock:
                        file = service.journal.export_event(int(path[9:]))
                        self.send(file.read_bytes(), content="text/markdown; charset=utf-8", filename=file.name)
                else:
                    if path == "/vendor/lucide.min.js":
                        file = vendor / "lucide.min.js"
                    else:
                        file = (static / ("index.html" if path == "/" else unquote(path.lstrip("/")))).resolve()
                        if not file.is_relative_to(static):
                            raise ValueError("not found")
                    if not file.is_file():
                        self.send({"error": "not found"}, 404)
                        return
                    self.send(file.read_bytes(), content=mimetypes.guess_type(str(file))[0] or "application/octet-stream")
            except (ValueError, OSError) as exc:
                self.send({"error": str(exc)}, 404)

        def do_POST(self):
            origin = self.headers.get("Origin")
            if not self.local() or self.headers.get("X-CSRF-Token") != token or (origin and origin != "http://" + self.headers.get("Host", "")):
                self.send({"error": "local request token required"}, 403)
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 <= length <= 16384:
                    raise ValueError("request too large")
                data = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(data, dict):
                    raise ValueError("JSON object required")
                path = urlsplit(self.path).path
                if path.startswith("/api/control/"):
                    result = service.control(path.rsplit("/", 1)[-1], data)
                elif path == "/api/settings":
                    result = service.update_settings(data)
                elif path == "/api/mode":
                    result = service.switch_mode(data.get("mode"))
                elif path == "/api/credentials":
                    result = service.save_credentials(data)
                elif path == "/api/check":
                    result = service.check_account()
                elif path == "/api/configure":
                    if data.get("confirmed") is not True:
                        raise ValueError("거래소 설정 변경 확인 필요")
                    result = service.configure_account()
                elif path == "/api/recover":
                    result = service.recover()
                elif path == "/api/backtest":
                    with job_lock:
                        if job["running"]:
                            raise ValueError("검증 실행 중")
                        with service.lock:
                            frames, settings = dict(service.frames), service.settings
                            if not frames or not service.quote:
                                raise ValueError("시세 수신 대기 중")
                            rules_ = dict(service.quote["rules"])
                            market, sample = service.market, service.sample
                            comparison = data.get("compare") is True
                        job.update(running=True, report=None, error=None)
                    def run_job():
                        try:
                            from .backtest import run, compare
                            from .market import Market, sample_frames
                            replay_frames, replay_rules = frames, rules_
                            if comparison and "15m" not in frames:
                                if sample:
                                    end = frames["1h"][-1].end
                                    replay_frames = {tf: [c for c in cs if c.end <= end]
                                                     for tf, cs in sample_frames(active=True).items()}
                                else:
                                    replay_frames, quote, _ = Market(market.base).load(active=True)
                                    replay_rules = quote["rules"]
                            timeline = replay_frames.get("15m") or replay_frames.get("30m") or replay_frames["1h"]
                            count = 480 if comparison else 240
                            funding = None if sample else market.funding(timeline[-min(count, len(timeline))].time, timeline[-1].end)
                            report = (compare if comparison else run)(replay_frames, settings, replay_rules, funding=funding, bars=count)
                            with job_lock:
                                job.update(running=False, report=report)
                        except Exception as exc:
                            with job_lock:
                                job.update(running=False, error=str(exc))
                    threading.Thread(target=run_job, name="elliott-validation", daemon=True).start()
                    result = {"ok": True}
                else:
                    self.send({"error": "not found"}, 404)
                    return
                self.send(result)
            except Exception as exc:
                self.send({"error": str(exc)}, 400 if isinstance(exc, (ValueError, ArithmeticError)) else 409)

        def log_message(self, *args):
            pass

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description="Elliott BTC local trading workstation")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data", default="data/elliott")
    parser.add_argument("--mode", choices=("paper", "testnet", "live"), default="paper")
    parser.add_argument("--sample", action="store_true")
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args(argv)
    service, server = None, None
    try:
        service = Service(args.data, args.mode, args.sample)
        server = ThreadingHTTPServer(("127.0.0.1", args.port), handler(service))
        server.daemon_threads = True
        service.start_worker()
        def stop(_sig, _frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, stop)
        url = f"http://127.0.0.1:{server.server_port}/"
        print(f"Elliott BTC: {url} | {args.mode} | manual start required", flush=True)
        if args.open:
            webbrowser.open(url)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"Elliott BTC: {exc}")
        return 2
    finally:
        if server:
            server.server_close()
        if service:
            service.close()
    return 0
