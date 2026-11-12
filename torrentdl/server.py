"""Tiny local web server: serves the UI and a JSON API for the engine.

Bound to 127.0.0.1 only, and every API call must carry a random per-launch
token, so other websites open in your browser can't talk to it.
"""
from __future__ import annotations

import base64
import json
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .engine import Engine


def web_dir() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "torrentdl" / "web"


class Server:
    def __init__(self, engine: Engine, port: int = 0):
        self.engine = engine
        self.token = secrets.token_urlsafe(24)
        handler = self._make_handler()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/?token={self.token}"

    def start(self) -> None:
        threading.Thread(target=self.httpd.serve_forever, name="http", daemon=True).start()

    def stop(self) -> None:
        self.httpd.shutdown()

    def _make_handler(self):
        srv = self
        eng = self.engine

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, obj, code: int = 200) -> None:
                self._send(code, json.dumps(obj).encode(), "application/json")

            def _authed(self) -> bool:
                q = parse_qs(urlparse(self.path).query)
                tok = self.headers.get("X-Token") or (q.get("token") or [""])[0]
                return secrets.compare_digest(tok, srv.token)

            def do_GET(self):
                u = urlparse(self.path)
                if u.path in ("/", "/index.html"):
                    self._send(200, (web_dir() / "index.html").read_bytes(), "text/html; charset=utf-8")
                elif u.path == "/api/state":
                    if not self._authed():
                        return self._json({"error": "forbidden"}, 403)
                    since = int((parse_qs(u.query).get("since") or ["0"])[0])
                    self._json(eng.snapshot(since))
                else:
                    self._json({"error": "not found"}, 404)

            def do_POST(self):
                if not self._authed():
                    return self._json({"error": "forbidden"}, 403)
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    return self._json({"error": "bad json"}, 400)
                path = urlparse(self.path).path
                save = body.get("save_path") or None

                if path == "/api/add":
                    added = 0
                    for m in body.get("magnets", []):
                        if m.strip():
                            added += eng.add_magnet(m, save)
                    for t in body.get("torrents", []):
                        try:
                            data = base64.b64decode(t["data"])
                        except Exception:
                            continue
                        added += eng.add_torrent_bytes(data, save, t.get("name", ""))
                    if body.get("paths"):
                        added += eng.add_paths(body["paths"], save)
                    return self._json({"added": added})
                if path == "/api/action":
                    return self._json({"ok": eng.action(body.get("id", ""), body.get("action", ""))})
                if path == "/api/all":
                    eng.all_action(body.get("action", ""))
                    return self._json({"ok": True})
                if path == "/api/settings":
                    return self._json(eng.update_settings(body))
                if path == "/api/open_folder":
                    from .engine import open_in_file_manager
                    p = Path(eng.settings["save_path"])
                    p.mkdir(parents=True, exist_ok=True)
                    open_in_file_manager(str(p))
                    return self._json({"ok": True})
                self._json({"error": "not found"}, 404)

        return H
