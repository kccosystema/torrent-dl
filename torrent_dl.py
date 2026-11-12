#!/usr/bin/env python3
"""torrent-dl — download your backed-up torrents in bulk.

Give it .torrent files, folders full of .torrent files, magnet links, or a
text file listing magnets (one per line). It queues everything, downloads into
one output folder, survives Ctrl-C / crashes (resume data), and re-uses any
data that is already on disk (it is hash-checked, not re-downloaded).

    python torrent_dl.py ~/backups/torrents -o ~/Downloads/restored
    python torrent_dl.py "magnet:?xt=urn:btih:..." -o ./out
    python torrent_dl.py magnets.txt one.torrent other/ -o ./out --seed-ratio 1.0

Built on libtorrent (pip install libtorrent).
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path

try:
    import libtorrent as lt
except ImportError:  # pragma: no cover
    sys.exit("libtorrent is not installed. Run:  pip install libtorrent")

STATE_NAMES = {
    0: "queued", 1: "checking", 2: "metadata", 3: "downloading",
    4: "finished", 5: "seeding", 6: "allocating", 7: "resume-chk",
}


# --------------------------------------------------------------------------- #
# input collection
# --------------------------------------------------------------------------- #
def collect_sources(items: list[str]) -> tuple[list[Path], list[str]]:
    """Expand CLI args into (.torrent paths, magnet URIs)."""
    torrents: list[Path] = []
    magnets: list[str] = []
    for item in items:
        if item.startswith("magnet:"):
            magnets.append(item)
            continue
        p = Path(item).expanduser()
        if p.is_dir():
            torrents += sorted(x for x in p.rglob("*") if x.suffix.lower() == ".torrent")
        elif p.suffix.lower() == ".torrent":
            torrents.append(p)
        elif p.is_file():  # treat as a list of magnets / torrent paths
            for line in p.read_text(errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("magnet:"):
                    magnets.append(line)
                elif line.lower().endswith(".torrent"):
                    torrents.append((p.parent / line).expanduser())
        else:
            print(f"! skipping {item}: not a file, folder or magnet link", file=sys.stderr)
    return torrents, magnets


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:6.1f}{unit}"
        n /= 1024
    return f"{n:6.1f}PB"


def eta(remaining: int, rate: int) -> str:
    if rate <= 0:
        return "   --:--"
    s = int(remaining / rate)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h:3d}:{m:02d}:{s:02d}" if h else f"   {m:02d}:{s:02d}"


def resume_key(h) -> str:
    ih = h.info_hashes()
    best = ih.get_best() if hasattr(ih, "get_best") else h.info_hash()
    return str(best)


# --------------------------------------------------------------------------- #
# downloader
# --------------------------------------------------------------------------- #
class Downloader:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.out = Path(args.output).expanduser().resolve()
        self.out.mkdir(parents=True, exist_ok=True)
        self.state_dir = self.out / ".torrent-dl"
        self.state_dir.mkdir(exist_ok=True)
        self.failed: dict[str, str] = {}
        self.done_logged: set[str] = set()
        self.stopping = False

        settings = {
            "listen_interfaces": f"0.0.0.0:{args.port},[::]:{args.port}",
            "alert_mask": (lt.alert_category.error | lt.alert_category.status
                           | lt.alert_category.storage),
            "enable_dht": not args.no_dht,
            "enable_lsd": not args.no_dht,
            "enable_upnp": not args.no_upnp,
            "enable_natpmp": not args.no_upnp,
            "active_downloads": args.max_active,
            "active_seeds": args.max_active if args.seed_ratio else 0,
            "active_limit": max(args.max_active * 2, 10),
            "download_rate_limit": int(args.down_limit * 1024),
            "upload_rate_limit": int(args.up_limit * 1024),
            "user_agent": "torrent-dl/1.0 libtorrent/" + lt.__version__,
            "dht_bootstrap_nodes": ("dht.libtorrent.org:25401,router.bittorrent.com:6881,"
                                    "router.utorrent.com:6881,dht.transmissionbt.com:6881"),
        }
        self.ses = lt.session(settings)

    # ---- adding -----------------------------------------------------------
    def _resume_path(self, key: str) -> Path:
        return self.state_dir / f"{key}.resume"

    def _apply_common(self, atp) -> None:
        atp.save_path = str(self.out)
        # auto-managed: libtorrent's queue decides what is active
        atp.flags |= lt.torrent_flags.auto_managed
        atp.flags &= ~lt.torrent_flags.paused

    def add_torrent_file(self, path: Path) -> None:
        try:
            ti = lt.torrent_info(str(path))
        except Exception as e:  # corrupt .torrent etc.
            self.failed[str(path)] = f"bad .torrent: {e}"
            return
        key = str(ti.info_hashes().get_best()) if hasattr(ti, "info_hashes") else str(ti.info_hash())
        atp = self._load_resume(key) or lt.add_torrent_params()
        atp.ti = ti
        self._apply_common(atp)
        self.ses.async_add_torrent(atp)

    def add_magnet(self, uri: str) -> None:
        try:
            atp = lt.parse_magnet_uri(uri)
        except Exception as e:
            self.failed[uri[:60]] = f"bad magnet: {e}"
            return
        key = str(atp.info_hashes.get_best()) if hasattr(atp, "info_hashes") else str(atp.info_hash)
        resumed = self._load_resume(key)
        if resumed is not None:
            atp = resumed  # contains metadata if we fetched it before
        self._apply_common(atp)
        self.ses.async_add_torrent(atp)

    def _load_resume(self, key: str):
        rp = self._resume_path(key)
        if not rp.exists():
            return None
        try:
            return lt.read_resume_data(rp.read_bytes())
        except Exception:
            return None

    # ---- alerts -----------------------------------------------------------
    def handle_alerts(self) -> int:
        """Process alerts. Returns how many resume-saves completed."""
        saved = 0
        for a in self.ses.pop_alerts():
            if isinstance(a, lt.add_torrent_alert):
                if a.error.value():
                    self.failed[a.params.name or "torrent"] = a.error.message()
                else:
                    for peer in self.args.peer:
                        host, _, port = peer.rpartition(":")
                        a.handle.connect_peer((host.strip("[]"), int(port)))
            elif isinstance(a, lt.save_resume_data_alert):
                key = resume_key(a.handle)
                data = lt.write_resume_data_buf(a.params)
                tmp = self._resume_path(key).with_suffix(".tmp")
                tmp.write_bytes(data)
                os.replace(tmp, self._resume_path(key))
                saved += 1
            elif isinstance(a, lt.save_resume_data_failed_alert):
                saved += 1
            elif isinstance(a, lt.metadata_received_alert):
                a.handle.save_resume_data(lt.torrent_handle.save_info_dict)
                # keep a .torrent copy so you never need the magnet again
                try:
                    ti = a.handle.torrent_file()
                    ct = lt.create_torrent(ti)
                    (self.state_dir / f"{ti.name()}.torrent").write_bytes(lt.bencode(ct.generate()))
                except Exception:
                    pass
            elif isinstance(a, lt.torrent_finished_alert):
                h = a.handle
                key = resume_key(h)
                if key not in self.done_logged:
                    self.done_logged.add(key)
                    self.log(f"✓ finished: {h.status().name}")
                h.save_resume_data(lt.torrent_handle.save_info_dict)
                if not self.args.seed_ratio:
                    h.pause()
            elif isinstance(a, (lt.file_error_alert, lt.torrent_error_alert)):
                name = a.handle.status().name if a.handle.is_valid() else "?"
                self.failed[name] = a.message()
                self.log(f"✗ error: {a.message()}")
        return saved

    def log(self, msg: str) -> None:
        sys.stdout.write("\r\033[K" + msg + "\n")
        sys.stdout.flush()

    # ---- main loop --------------------------------------------------------
    def run(self, torrents: list[Path], magnets: list[str]) -> int:
        for t in torrents:
            self.add_torrent_file(t)
        for m in magnets:
            self.add_magnet(m)
        expected = len(torrents) + len(magnets) - len(self.failed)
        print(f"Queued {expected} torrent(s) → {self.out}")
        print(f"  (max {self.args.max_active} active at once; Ctrl-C to stop — progress is saved)\n")

        def _stop(*_):
            self.stopping = True
        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)

        last_save = time.time()
        last_draw = 0.0
        while not self.stopping:
            self.handle_alerts()
            handles = self.ses.get_torrents()
            statuses = [h.status() for h in handles]

            if time.time() - last_save > 60:  # periodic checkpoint
                for h in handles:
                    if h.need_save_resume_data():
                        h.save_resume_data(lt.torrent_handle.save_info_dict)
                last_save = time.time()

            if time.time() - last_draw >= 1:
                self.draw(statuses)
                last_draw = time.time()

            if handles and len(handles) >= expected and self.all_done(statuses):
                break
            time.sleep(0.25)

        return self.shutdown()

    def all_done(self, statuses) -> bool:
        for s in statuses:
            if s.errc.value():
                continue
            if not s.is_finished:
                return False
            if self.args.seed_ratio:
                ratio = s.all_time_upload / max(s.total_wanted, 1)
                if ratio < self.args.seed_ratio:
                    return False
        return True

    def draw(self, statuses) -> None:
        if not sys.stdout.isatty() and not self.args.verbose:
            return
        dl = sum(s.download_payload_rate for s in statuses)
        ul = sum(s.upload_payload_rate for s in statuses)
        wanted = sum(s.total_wanted for s in statuses)
        have = sum(s.total_wanted_done for s in statuses)
        n_done = sum(1 for s in statuses if s.is_finished)
        peers = sum(s.num_peers for s in statuses)
        pct = 100 * have / wanted if wanted else 0.0
        bar_w = 30
        fill = int(bar_w * pct / 100)
        line = (f"[{'#' * fill}{'.' * (bar_w - fill)}] {pct:5.1f}%  "
                f"{n_done}/{len(statuses)} done  ↓{human(dl)}/s ↑{human(ul)}/s  "
                f"peers {peers:3d}  ETA {eta(wanted - have, dl)}")
        if self.args.verbose:
            active = [s for s in statuses if not s.is_finished and not s.paused]
            for s in active[:8]:
                self.log(f"  {STATE_NAMES.get(int(s.state), '?'):11s} {s.progress*100:5.1f}% "
                         f"↓{human(s.download_payload_rate)}/s p{s.num_peers:<3d} {s.name[:60]}")
        sys.stdout.write("\r\033[K" + line)
        sys.stdout.flush()

    def shutdown(self) -> int:
        print("\n\nSaving progress…")
        self.ses.pause()
        handles = [h for h in self.ses.get_torrents() if h.is_valid() and h.status().has_metadata]
        for h in handles:
            h.save_resume_data(lt.torrent_handle.save_info_dict | lt.torrent_handle.flush_disk_cache)
        pending, deadline = len(handles), time.time() + 30
        while pending > 0 and time.time() < deadline:
            self.ses.wait_for_alert(500)
            pending -= self.handle_alerts()

        statuses = [h.status() for h in self.ses.get_torrents()]
        done = [s for s in statuses if s.is_finished]
        todo = [s for s in statuses if not s.is_finished]
        print(f"\nComplete:   {len(done)}")
        print(f"Incomplete: {len(todo)}")
        for s in todo:
            what = "waiting for metadata (no peers?)" if not s.has_metadata else f"{s.progress*100:.1f}%"
            print(f"   - {s.name or str(s.info_hashes.get_best())}: {what}, seen {s.list_seeds} seed(s)")
        if self.failed:
            print(f"Failed:     {len(self.failed)}")
            for k, v in self.failed.items():
                print(f"   - {k}: {v}")
        if todo:
            print("\nRe-run the same command to continue where you left off.")
        return 0 if not todo and not self.failed else 1


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Bulk-download torrents (.torrent files, folders, magnet links, or lists).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("sources", nargs="+",
                    help=".torrent files, folders (searched recursively), magnet links, "
                         "or text files with one magnet/.torrent path per line")
    ap.add_argument("-o", "--output", default="./downloads", help="where files are saved")
    ap.add_argument("-j", "--max-active", type=int, default=5,
                    help="how many torrents download at the same time")
    ap.add_argument("-p", "--port", type=int, default=6881, help="listen port for incoming peers")
    ap.add_argument("--down-limit", type=float, default=0, help="download cap in KB/s (0 = none)")
    ap.add_argument("--up-limit", type=float, default=0, help="upload cap in KB/s (0 = none)")
    ap.add_argument("--seed-ratio", type=float, default=0,
                    help="keep seeding until this upload ratio before exiting (0 = stop when done)")
    ap.add_argument("--peer", action="append", default=[], metavar="HOST:PORT",
                    help="also connect directly to this peer (e.g. your own seedbox/NAS); repeatable")
    ap.add_argument("--no-dht", action="store_true", help="disable DHT/LSD peer discovery")
    ap.add_argument("--no-upnp", action="store_true", help="don't try to open router ports")
    ap.add_argument("-v", "--verbose", action="store_true", help="show per-torrent lines")
    args = ap.parse_args(argv)

    torrents, magnets = collect_sources(args.sources)
    if not torrents and not magnets:
        ap.error("nothing to download — no .torrent files or magnet links found")
    return Downloader(args).run(torrents, magnets)


if __name__ == "__main__":
    sys.exit(main())
