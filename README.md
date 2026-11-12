# torrent-dl

Bulk downloader for old torrent backups. Point it at a folder of `.torrent`
files (and/or magnet links) and it fetches everything into one place.

## Install

```bash
python3 -m pip install -r requirements.txt     # Python 3.9+; installs libtorrent
```

## Use

```bash
# every .torrent under a folder (recursive)
python3 torrent_dl.py ~/backups/torrents -o ~/Restored

# mix and match: files, folders, magnet links, a text file of magnets
python3 torrent_dl.py old.torrent ~/more-torrents magnets.txt "magnet:?xt=urn:btih:..." -o ~/Restored

# pull straight from your own box/NAS/seedbox that still has the data
python3 torrent_dl.py ~/backups/torrents -o ~/Restored --peer 192.168.1.20:51413
```

A list file has one magnet link or `.torrent` path per line; `#` lines are ignored.

| Option | Default | What it does |
| --- | --- | --- |
| `-o DIR` | `./downloads` | Where files go |
| `-j N` | `5` | Torrents downloading at once (the rest wait in a queue) |
| `-p PORT` | `6881` | Listen port. Forward it on your router for more peers |
| `--down-limit` / `--up-limit` | none | Speed caps in KB/s |
| `--seed-ratio R` | `0` | Keep uploading until ratio R before exiting (0 = stop when done) |
| `--peer HOST:PORT` | – | Connect straight to a known peer; can be repeated |
| `--no-dht` | off | Turn off DHT/LSD (trackers + `--peer` only) |
| `--no-upnp` | off | Don't ask the router to open the port |
| `-v` | off | Per-torrent status lines |

## Good to know

- **Stop any time.** Ctrl-C saves progress to `DIR/.torrent-dl/`. Run the same
  command again and it carries on. It also checkpoints every minute, so a crash
  costs a minute at most.
- **Data you already have is reused.** If some files are already in the output
  folder, they're hash-checked and only missing or damaged pieces are downloaded.
  That makes it a handy way to repair half-broken backups too.
- **Magnet links get saved as `.torrent` files.** Once a magnet's metadata
  arrives, a `.torrent` copy goes into `DIR/.torrent-dl/`, so you won't need
  the magnet link again.
- **Old torrents need someone seeding.** A `.torrent` file only describes the
  data; somebody has to still be sharing it. The summary at the end lists
  anything that couldn't complete and how many seeds were seen. Torrents with
  zero seeds can't finish until a seed shows up. If you still have the data on
  another machine, run any client there and point this one at it with `--peer`.
- Exit code is `0` when everything finished and `1` if anything's left over,
  so it works from scripts and cron jobs.
