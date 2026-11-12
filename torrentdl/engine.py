"""Download engine: a libtorrent session plus persistence.

Everything the app knows lives in a per-user state folder:
  settings.json            user settings
  resume/<id>.resume       one file per torrent (libtorrent resume data,
                           including metadata and save location)
so closing and reopening the app picks up exactly where it left off.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import libtorrent as lt

DEFAULTS = {
    "save_path": str(Path.home() / "Downloads" / "TorrentDL"),
    "max_active": 5,
    "down_limit": 0,      # KB/s, 0 = unlimited
    "up_limit": 0,        # KB/s, 0 = unlimited
    "keep_seeding": False,
    "port": 6881,
}

CHECKING = {1, 7}          # checking_files, checking_resume_data
METADATA = 2               # downloading_metadata


def default_state_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "TorrentDL"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "TorrentDL"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "torrent-dl"


def handle_id(h) -> str:
    ih = h.info_hashes()
    return str(ih.v1) if ih.has_v1() else str(ih.v2)


def open_in_file_manager(path: str) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


class Engine:
    def __init__(self, state_dir: Path | None = None, listen: str | None = None):
        self.state_dir = Path(state_dir or default_state_dir())
        self.resume_dir = self.state_dir / "resume"
        self.resume_dir.mkdir(parents=True, exist_ok=True)
        self.settings = dict(DEFAULTS)
        self._load_settings()

        self.messages: list[dict] = []          # toasts for the UI
        self._msg_seq = 0
        self._lock = threading.Lock()
        self._removed: set[str] = set()
        self._stop = threading.Event()

        port = int(self.settings["port"])
        self.ses = lt.session({
            "listen_interfaces": listen or f"0.0.0.0:{port},[::]:{port}",
            "alert_mask": (lt.alert_category.error | lt.alert_category.status
                           | lt.alert_category.storage),
            "user_agent": "TorrentDL/1.0 libtorrent/" + lt.__version__,
            "dht_bootstrap_nodes": ("dht.libtorrent.org:25401,router.bittorrent.com:6881,"
                                    "router.utorrent.com:6881,dht.transmissionbt.com:6881"),
        })
        self._apply_settings()
        self._load_saved_torrents()
        self._thread = threading.Thread(target=self._loop, name="engine", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ settings
    def _load_settings(self) -> None:
        f = self.state_dir / "settings.json"
        if f.exists():
            try:
                self.settings.update(json.loads(f.read_text()))
            except Exception:
                pass

    def update_settings(self, new: dict) -> dict:
        for k in DEFAULTS:
            if k in new:
                self.settings[k] = type(DEFAULTS[k])(new[k])
        (self.state_dir / "settings.json").write_text(json.dumps(self.settings, indent=2))
        self._apply_settings()
        return self.settings

    def _apply_settings(self) -> None:
        s = self.settings
        n = max(1, int(s["max_active"]))
        self.ses.apply_settings({
            "active_downloads": n,
            "active_seeds": n if s["keep_seeding"] else 0,
            "active_limit": max(2 * n, 10),
            "download_rate_limit": int(float(s["down_limit"]) * 1024),
            "upload_rate_limit": int(float(s["up_limit"]) * 1024),
        })

    # ------------------------------------------------------------------ messages
    def notify(self, text: str, kind: str = "info") -> None:
        with self._lock:
            self._msg_seq += 1
            self.messages.append({"id": self._msg_seq, "text": text, "kind": kind})
            self.messages = self.messages[-50:]

    # ------------------------------------------------------------------ adding
    def _prepare(self, atp, save_path: str | None) -> None:
        atp.save_path = save_path or self.settings["save_path"]
        Path(atp.save_path).mkdir(parents=True, exist_ok=True)
        atp.flags |= lt.torrent_flags.auto_managed
        atp.flags &= ~lt.torrent_flags.paused
        atp.flags &= ~lt.torrent_flags.duplicate_is_error

    def add_torrent_bytes(self, data: bytes, save_path: str | None = None, label: str = "") -> bool:
        try:
            ti = lt.torrent_info(lt.bdecode(data))
        except Exception as e:
            self.notify(f"Couldn't read {label or 'torrent file'}: {e}", "error")
            return False
        atp = lt.add_torrent_params()
        atp.ti = ti
        self._prepare(atp, save_path)
        self.ses.async_add_torrent(atp)
        return True

    def add_torrent_path(self, path: Path, save_path: str | None = None) -> bool:
        try:
            return self.add_torrent_bytes(Path(path).read_bytes(), save_path, Path(path).name)
        except OSError as e:
            self.notify(f"Couldn't open {path}: {e}", "error")
            return False

    def add_magnet(self, uri: str, save_path: str | None = None) -> bool:
        try:
            atp = lt.parse_magnet_uri(uri.strip())
        except Exception:
            self.notify(f"Not a valid magnet link: {uri[:50]}…", "error")
            return False
        self._prepare(atp, save_path)
        self.ses.async_add_torrent(atp)
        return True

    def add_paths(self, paths: list[str], save_path: str | None = None) -> int:
        """Add .torrent files and/or folders (searched recursively)."""
        n = 0
        for p in map(Path, paths):
            if p.is_dir():
                for f in sorted(p.rglob("*")):
                    if f.suffix.lower() == ".torrent" and f.is_file():
                        n += self.add_torrent_path(f, save_path)
            elif p.is_file():
                if p.suffix.lower() == ".torrent":
                    n += self.add_torrent_path(p, save_path)
                else:  # list of magnets
                    for line in p.read_text(errors="replace").splitlines():
                        if line.strip().startswith("magnet:"):
                            n += self.add_magnet(line, save_path)
        return n

    def _load_saved_torrents(self) -> None:
        for f in sorted(self.resume_dir.glob("*.resume")):
            try:
                atp = lt.read_resume_data(f.read_bytes())
                self.ses.async_add_torrent(atp)
            except Exception:
                f.rename(f.with_suffix(".bad"))

    # ------------------------------------------------------------------ actions
    def _find(self, tid: str):
        for h in self.ses.get_torrents():
            if handle_id(h) == tid:
                return h
        return None

    def action(self, tid: str, action: str) -> bool:
        h = self._find(tid)
        if h is None:
            return False
        if action == "pause":
            h.unset_flags(lt.torrent_flags.auto_managed)
            h.pause()
        elif action == "resume":
            if h.status().is_finished and not self.settings["keep_seeding"]:
                h.force_recheck()  # "resume" on a finished one = verify the files again
            h.set_flags(lt.torrent_flags.auto_managed)
            h.resume()
        elif action == "recheck":
            h.force_recheck()
        elif action == "open":
            s = h.status()
            target = Path(s.save_path) / s.name if s.name else Path(s.save_path)
            open_in_file_manager(str(target if target.exists() else s.save_path))
            return True
        elif action in ("remove", "delete"):
            self._removed.add(tid)
            flags = lt.session.delete_files if action == "delete" else 0
            self.ses.remove_torrent(h, flags)
            for f in self.resume_dir.glob(f"{tid}.*"):
                f.unlink(missing_ok=True)
            return True
        else:
            return False
        h.save_resume_data(lt.torrent_handle.save_info_dict)
        return True

    def all_action(self, action: str) -> None:
        for h in self.ses.get_torrents():
            s = h.status()
            if action == "pause" and not s.paused:
                self.action(handle_id(h), "pause")
            elif action == "resume" and s.paused and not s.is_finished:
                self.action(handle_id(h), "resume")
            elif action == "clear_done" and s.is_finished:
                self.action(handle_id(h), "remove")

    # ------------------------------------------------------------------ status
    @staticmethod
    def _label(s) -> str:
        if s.errc.value():
            return "Error"
        if s.is_finished:
            return "Seeding" if not s.paused else "Done"
        if s.paused:
            return "Queued" if (s.flags & lt.torrent_flags.auto_managed) else "Paused"
        st = int(s.state)
        if st in CHECKING:
            return "Checking files"
        if st == METADATA:
            return "Getting torrent info"
        return "Downloading" if s.num_peers else "Looking for peers"

    def snapshot(self, since_msg: int = 0) -> dict:
        items = []
        tot_dl = tot_ul = tot_size = tot_done = 0
        for h in self.ses.get_torrents():
            if not h.is_valid():
                continue
            s = h.status()
            tid = handle_id(h)
            if tid in self._removed:
                continue
            size, done = s.total_wanted, s.total_wanted_done
            rate = s.download_payload_rate
            items.append({
                "id": tid,
                "name": s.name or tid,
                "status": self._label(s),
                "progress": s.progress if not s.is_finished else 1.0,
                "size": size, "done": done,
                "down": rate, "up": s.upload_payload_rate,
                "peers": s.num_peers, "seeds": s.num_seeds,
                "seeds_seen": max(s.list_seeds, s.num_complete if s.num_complete > 0 else 0),
                "eta": int((size - done) / rate) if rate > 0 and size > done else None,
                "save_path": s.save_path,
                "error": s.errc.message() if s.errc.value() else "",
                "paused": bool(s.paused),
                "finished": bool(s.is_finished),
                "has_metadata": bool(s.has_metadata),
                "added": s.added_time,
            })
            tot_dl += rate
            tot_ul += s.upload_payload_rate
            tot_size += size
            tot_done += done
        items.sort(key=lambda t: (t["finished"], -t["added"], t["name"].lower()))
        with self._lock:
            msgs = [m for m in self.messages if m["id"] > since_msg]
        return {
            "torrents": items,
            "totals": {"down": tot_dl, "up": tot_ul, "size": tot_size, "done": tot_done,
                       "count": len(items), "finished": sum(t["finished"] for t in items)},
            "settings": self.settings,
            "messages": msgs,
            "listen_port": self.ses.listen_port(),
        }

    # ------------------------------------------------------------------ loop
    def _save(self, a) -> None:
        tid = handle_id(a.handle)
        if tid in self._removed:
            return
        path = self.resume_dir / f"{tid}.resume"
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(lt.write_resume_data_buf(a.params))
        os.replace(tmp, path)

    def _handle_alerts(self) -> int:
        saved = 0
        for a in self.ses.pop_alerts():
            try:
                if isinstance(a, lt.add_torrent_alert):
                    if a.error.value():
                        self.notify(f"Couldn't add {a.params.name or 'torrent'}: {a.error.message()}", "error")
                    else:
                        self._removed.discard(handle_id(a.handle))
                        a.handle.save_resume_data(lt.torrent_handle.save_info_dict)
                elif isinstance(a, lt.save_resume_data_alert):
                    self._save(a)
                    saved += 1
                elif isinstance(a, lt.save_resume_data_failed_alert):
                    saved += 1
                elif isinstance(a, lt.metadata_received_alert):
                    a.handle.save_resume_data(lt.torrent_handle.save_info_dict)
                elif isinstance(a, lt.torrent_finished_alert):
                    h = a.handle
                    s = h.status()
                    # torrent_finished also fires when a finished torrent is re-loaded;
                    # only announce real completions
                    if s.total_payload_download > 0:
                        self.notify(f"Finished: {s.name}", "success")
                    if not self.settings["keep_seeding"]:
                        h.unset_flags(lt.torrent_flags.auto_managed)
                        h.pause()
                    h.save_resume_data(lt.torrent_handle.save_info_dict)
                elif isinstance(a, lt.file_error_alert):
                    self.notify(f"File problem: {a.message()}", "error")
            except Exception as e:  # never let one alert kill the loop
                self.notify(f"Internal error: {e}", "error")
        return saved

    def _loop(self) -> None:
        last_ckpt = time.time()
        while not self._stop.is_set():
            self.ses.wait_for_alert(500)
            self._handle_alerts()
            if time.time() - last_ckpt > 60:
                for h in self.ses.get_torrents():
                    if h.need_save_resume_data():
                        h.save_resume_data(lt.torrent_handle.save_info_dict)
                last_ckpt = time.time()

    def shutdown(self, timeout: float = 20) -> None:
        """Pause everything and write resume data before the app exits."""
        self._stop.set()
        self._thread.join(timeout=2)
        self.ses.pause()
        handles = [h for h in self.ses.get_torrents() if h.is_valid()]
        for h in handles:
            h.save_resume_data(lt.torrent_handle.save_info_dict | lt.torrent_handle.flush_disk_cache)
        pending, deadline = len(handles), time.time() + timeout
        while pending > 0 and time.time() < deadline:
            self.ses.wait_for_alert(500)
            pending -= self._handle_alerts()
