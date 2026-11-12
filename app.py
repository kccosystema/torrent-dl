#!/usr/bin/env python3
"""TorrentDL desktop app entry point.

Opens a native window (via pywebview). If that isn't available it falls back
to opening the UI in your normal web browser.
"""
from __future__ import annotations

import argparse
import signal
import json
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

from torrentdl import __version__
from torrentdl.engine import Engine
from torrentdl.server import Server


class Api:
    """Functions the page can call through window.pywebview.api (native dialogs)."""

    def __init__(self):
        self.window = None

    def _dialog(self, kind, **kw):
        import webview
        res = self.window.create_file_dialog(kind, **kw)
        if not res:
            return []
        return [res] if isinstance(res, str) else list(res)

    def choose_folder(self):
        import webview
        r = self._dialog(webview.FOLDER_DIALOG)
        return r[0] if r else None

    def choose_torrent_files(self):
        import webview
        return self._dialog(webview.OPEN_DIALOG, allow_multiple=True,
                            file_types=("Torrent files (*.torrent)", "All files (*.*)"))


def selftest(engine: Engine, server: Server) -> int:
    """Used by CI on the packaged app: start everything, hit the API, exit."""
    with urllib.request.urlopen(f"http://127.0.0.1:{server.port}/api/state?token={server.token}") as r:
        state = json.load(r)
    with urllib.request.urlopen(f"http://127.0.0.1:{server.port}/") as r:
        assert b"TorrentDL" in r.read()
    window = "n/a"
    if sys.platform in ("win32", "darwin"):
        try:  # make sure the native window toolkit made it into the bundle
            import webview  # noqa: F401
            if sys.platform == "win32":
                from webview.platforms import winforms  # noqa: F401
            else:
                from webview.platforms import cocoa  # noqa: F401
            window = "ok"
        except Exception as e:
            return _report(engine, f"selftest FAILED: native window support missing: {e!r}", 2)
    return _report(engine, f"selftest ok: window {window}, libtorrent {__import__('libtorrent').__version__}, "
                           f"listening on {state['listen_port']}, {len(state['torrents'])} torrents", 0)


def _report(engine: Engine, msg: str, code: int) -> int:
    # windowed builds have no console on Windows, so also leave the result in a file
    print(msg)
    (engine.state_dir / "selftest.txt").write_text(msg + "\n")
    return code


def main() -> int:
    ap = argparse.ArgumentParser(prog="TorrentDL")
    ap.add_argument("--browser", action="store_true", help="use your web browser instead of an app window")
    ap.add_argument("--state-dir", help=argparse.SUPPRESS)
    ap.add_argument("--selftest", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--version", action="version", version=__version__)
    args, _ = ap.parse_known_args()  # macOS may pass -psn_… args

    engine = Engine(Path(args.state_dir) if args.state_dir else None)
    server = Server(engine)
    server.start()

    try:
        if args.selftest:
            return selftest(engine, server)

        if not args.browser:
            try:
                import webview
            except Exception:
                webview = None
            if webview is not None:
                api = Api()
                api.window = webview.create_window(
                    "TorrentDL", server.url, js_api=api,
                    width=1040, height=720, min_size=(640, 480))
                webview.start()
                return 0

        def _quit(*_):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, _quit)
        signal.signal(signal.SIGINT, _quit)
        print(f"TorrentDL is running at {server.url}\nClose this window (or press Ctrl-C) to quit.")
        webbrowser.open(server.url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        return 0
    finally:
        engine.shutdown()
        server.stop()


if __name__ == "__main__":
    sys.exit(main())
