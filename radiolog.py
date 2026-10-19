#!/usr/bin/env python3
"""
radiolog - log and browse what your internet radio played.

Two ways to capture "Artist - Title":

  1) Stream mode (any OS, most reliable)
     Connects to the radio stream URL itself and reads the ICY metadata
     (StreamTitle) that Shoutcast/Icecast stations broadcast.
         python radiolog.py watch --url http://stream.example.com:8000/radio

  2) System mode (reads whatever your media player reports as "now playing")
     Windows: uses the system media session (the same info as the volume popup).
              pip install winrt-runtime winrt-Windows.Media.Control winrt-Windows.Foundation winrt-Windows.Foundation.Collections winrt-Windows.Storage.Streams
     Linux:   uses MPRIS through `playerctl` (install it with your package manager).
         python radiolog.py watch --system

Browse the history:
     python radiolog.py show                 # last 50 songs
     python radiolog.py show --today
     python radiolog.py show --since 3h
     python radiolog.py show --since "2026-10-18 20:00" --until "2026-10-18 23:00"
     python radiolog.py show --search queen
     python radiolog.py show --today --csv today.csv

On first start radiolog asks where to store the database. Change it later with
    radiolog config --db NEW_LOCATION
(or override per run with the RADIOLOG_DB environment variable).
"""
import argparse
import csv
import json
import os
import platform
import re
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlsplit

IS_WIN = sys.platform == "win32"
DEFAULT_DB = Path.home() / ".radiolog.db"   # default: in the user folder (no admin rights needed)
DB_PATH = None                              # set by resolve_db_path() at startup
USER_AGENT = "radiolog/1.0"
WIN_PKGS = "winrt-runtime winrt-Windows.Media.Control winrt-Windows.Foundation winrt-Windows.Foundation.Collections winrt-Windows.Storage.Streams"


# --------------------------------------------------------------------------- database location

def config_file():
    """Settings file, kept in a per-user folder that never needs admin rights."""
    if IS_WIN:
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "radiolog" / "config.json"


def load_config():
    try:
        return json.loads(config_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def is_admin():
    try:
        if IS_WIN:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        return os.geteuid() == 0
    except Exception:
        return False


def admin_words():
    if IS_WIN:
        return ("administrator rights",
                "run radiolog from a terminal opened with \"Run as administrator\"")
    return ("root rights", "run radiolog with sudo")


def normalize_db_input(text):
    """User input (folder or file, maybe quoted, may contain ~ or %VARS%) -> absolute db file path."""
    text = text.strip().strip('"').strip("'").strip()
    path = Path(os.path.expandvars(os.path.expanduser(text)))
    if path.is_dir() or text.endswith(("/", "\\")) or not path.suffix:
        path = path / "radiolog.db"
    return path.resolve()


def check_writable(db_path):
    """Can this user create/update the database here? Returns (ok, error)."""
    folder = db_path.parent
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / f".radiolog-write-test-{os.getpid()}"
        probe.write_bytes(b"")      # SQLite also needs to create temp files next to the db
        probe.unlink()
        if db_path.exists():
            with open(db_path, "ab"):
                pass
        return True, None
    except OSError as e:
        return False, e


def ask(prompt, default=""):
    try:
        answer = input(prompt).strip()
    except EOFError:
        answer = ""
    return answer or default


def count_songs(db_path):
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        n = con.execute("SELECT COUNT(*) FROM plays").fetchone()[0]
        con.close()
        return n
    except sqlite3.Error:
        return 0


def choose_db_location(default):
    """Interactive: ask for a location until it's usable (or the user accepts needing elevation).
    Returns (path, needs_elevation)."""
    need, how = admin_words()
    examples = ("your user folder, Documents, or another drive such as D:\\Music\\radiolog"
                if IS_WIN else "your home folder, e.g. ~/Music/radiolog")
    protected = ("C:\\Program Files, C:\\Windows or the root of C:\\"
                 if IS_WIN else "/usr, /opt, /var or /etc")
    print(
        "Where should radiolog store your song history (the database file)?\n\n"
        f"  Choose a location you can write to WITHOUT {need}, like\n"
        f"  {examples}.\n\n"
        f"  Locations such as {protected} need {need}.\n"
        f"  If you pick one of those, you will have to {how}\n"
        "  EVERY time you use it.\n\n"
        "  Enter a folder or a full file path, or press Enter for the default\n"
        "  (your user folder).\n"
    )
    while True:
        db_path = normalize_db_input(ask(f"Database location [{default}]: ", str(default)))
        ok, err = check_writable(db_path)
        if ok:
            if is_admin():
                print(f"\n  Note: this terminal has {need}, so I can't tell whether the location\n"
                      f"  needs them. If it does, you'll have to {how} every time.")
            return db_path, False
        print(f"\n✖ radiolog can't write to {db_path.parent}\n  ({err.strerror or err})\n"
              f"  This location needs {need}.\n\n"
              "  [1] choose a different location (recommended)\n"
              f"  [2] keep it: I'll {how} every time\n")
        if ask("Your choice [1]: ", "1") == "2":
            return db_path, True
        print()


def offer_move(old, new):
    """Move an existing history file to the newly chosen place (asks first)."""
    if not old.exists() or old.resolve() == new.resolve() or new.exists():
        return
    n = count_songs(old)
    if ask(f"Move your existing history ({n} songs) from {old} there? [Y/n]: ", "y").lower() \
            not in ("y", "yes", "t", "tak"):
        print(f"  Left it where it was: {old}")
        return
    try:
        new.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old), str(new))
        print(f"  Moved ✔")
    except OSError as e:
        print(f"  Couldn't move it ({e}). Your old history stays at {old}")


def first_run_setup():
    print("\n── radiolog first-time setup ───────────────────────────\n")
    default = DEFAULT_DB
    if DEFAULT_DB.exists():
        print(f"Found your existing history at {DEFAULT_DB} ({count_songs(DEFAULT_DB)} songs).\n"
              "Press Enter to keep using it, or choose a new place to move it to.\n")
    db_path, needs_elevation = choose_db_location(default)
    offer_move(DEFAULT_DB, db_path)
    save_config({"db_path": str(db_path)})
    print(f"\n✔ Saved. Your song history will be stored in:\n  {db_path}\n"
          f"  (change it any time with:  radiolog config --db NEW_LOCATION)\n")
    if needs_elevation:
        _, how = admin_words()
        raise SystemExit(f"This location needs elevated rights, so {how} from now on.")
    print("─" * 56 + "\n")
    return db_path


def resolve_db_path():
    """RADIOLOG_DB env variable > saved setting > first-run question (> legacy default)."""
    if os.environ.get("RADIOLOG_DB"):
        return Path(os.environ["RADIOLOG_DB"]).expanduser().resolve()
    saved = load_config().get("db_path")
    if saved:
        return Path(saved)
    if sys.stdin.isatty():
        return first_run_setup()
    return DEFAULT_DB       # no one to ask (e.g. started by a script): use the default, ask later


# --------------------------------------------------------------------------- storage

def db_connect():
    ok, err = check_writable(DB_PATH)
    if not ok:
        need, how = admin_words()
        raise SystemExit(
            f"radiolog can't write to its database at:\n  {DB_PATH}\n  ({err.strerror or err})\n\n"
            f"This location needs {need}. Either {how},\n"
            "or move the history somewhere you can write to:\n"
            "  radiolog config --db NEW_LOCATION"
        )
    con = sqlite3.connect(DB_PATH)
    con.execute(
        """CREATE TABLE IF NOT EXISTS plays (
               id        INTEGER PRIMARY KEY,
               played_at TEXT NOT NULL,      -- local time, ISO format
               artist    TEXT,
               title     TEXT NOT NULL,
               raw       TEXT,               -- metadata exactly as received
               source    TEXT                -- station name / player app
           )"""
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_played_at ON plays(played_at)")
    return con


def split_artist_title(raw):
    """'Artist - Title' -> ('Artist', 'Title'). Falls back to ('', raw)."""
    raw = (raw or "").strip()
    for sep in (" - ", " – ", " — "):
        if sep in raw:
            artist, title = raw.split(sep, 1)
            return artist.strip(), title.strip()
    return "", raw


class Recorder:
    """Saves a song only when it differs from the previous one."""

    def __init__(self, con):
        self.con = con
        self.last = None
        # Don't double-log the current song if the tool was just restarted.
        row = con.execute(
            "SELECT played_at, artist, title FROM plays ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row and datetime.now() - datetime.fromisoformat(row[0]) < timedelta(minutes=10):
            self.last = ((row[1] or "").lower(), row[2].lower())

    def header(self):
        """Blank line + 'Discovered' heading between the status lines and the songs (once)."""
        if not getattr(self, "_header_done", False):
            self._header_done = True
            print("\n── Discovered ──────────────────────────────\n", flush=True)

    def record(self, artist, title, raw, source):
        artist, title = (artist or "").strip(), (title or "").strip()
        if not title and not artist:
            return
        if not title:  # some stations put everything in one field
            title, artist = artist, ""
        key = (artist.lower(), title.lower())
        if key == self.last:
            return
        self.last = key
        now = datetime.now().isoformat(timespec="seconds")
        self.con.execute(
            "INSERT INTO plays (played_at, artist, title, raw, source) VALUES (?,?,?,?,?)",
            (now, artist, title, raw, source),
        )
        self.con.commit()
        print(f"[{now.replace('T', ' ')}] {artist or '?'} — {title}", flush=True)


# --------------------------------------------------------------------------- stream (ICY) mode

def read_exact(f, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = f.read(n - len(buf))
        if not chunk:
            raise ConnectionError("stream ended")
        buf += chunk
    return bytes(buf)


def first_url_in_playlist(body):
    if "#EXT-X-" in body:
        raise SystemExit(
            "This is an HLS (.m3u8) stream, which carries no ICY metadata.\n"
            "Use --system mode instead, or find the station's direct MP3/AAC stream URL."
        )
    for line in body.splitlines():
        line = line.strip()
        if re.match(r"(?i)^file\d+=", line):
            line = line.split("=", 1)[1].strip()
        if re.match(r"(?i)^https?://", line):
            return line
    raise ConnectionError("playlist contained no stream URL")


def open_stream(url, max_hops=6):
    """Open a stream asking for ICY metadata. Follows redirects and .pls/.m3u playlists.
    Uses a raw socket because old Shoutcast servers answer 'ICY 200 OK',
    which Python's http.client refuses to parse."""
    for _ in range(max_hops):
        u = urlsplit(url)
        if u.scheme not in ("http", "https"):
            raise SystemExit(f"Unsupported URL: {url}")
        port = u.port or (443 if u.scheme == "https" else 80)
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")

        sock = socket.create_connection((u.hostname, port), timeout=30)
        if u.scheme == "https":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=u.hostname)
        sock.sendall(
            (f"GET {path} HTTP/1.0\r\nHost: {u.netloc}\r\nUser-Agent: {USER_AGENT}\r\n"
             "Icy-MetaData: 1\r\nAccept: */*\r\nConnection: close\r\n\r\n").encode()
        )
        f = sock.makefile("rb")

        status = f.readline().decode("latin-1").strip()
        headers = {}
        while True:
            line = f.readline()
            if not line or line in (b"\r\n", b"\n"):
                break
            k, _, v = line.decode("latin-1").partition(":")
            headers[k.strip().lower()] = v.strip()

        parts = status.split()
        code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0

        if code in (301, 302, 303, 307, 308) and "location" in headers:
            f.close(); sock.close()
            url = urljoin(url, headers["location"])
            continue
        if code != 200:
            f.close(); sock.close()
            raise ConnectionError(f"server answered '{status}'")

        ctype = headers.get("content-type", "").lower()
        if ctype.startswith(("text/html", "application/xhtml")):
            f.close(); sock.close()
            raise SystemExit(
                f"{url} is a web page, not an audio stream.\n"
                "If you listen to this radio in your browser, use:  radiolog watch --system\n"
                "Or find the station's direct stream link (often ends in .mp3, .aac, .pls or .m3u)."
            )
        if any(x in ctype for x in ("mpegurl", "scpls", "x-pls")) or \
                u.path.lower().endswith((".m3u", ".m3u8", ".pls")):
            body = f.read(65536).decode("utf-8", "replace")
            f.close(); sock.close()
            url = urljoin(url, first_url_in_playlist(body))
            continue

        return f, sock, headers, url
    raise ConnectionError("too many redirects/playlists")


def decode_meta(data, encoding):
    if encoding != "auto":
        return data.decode(encoding, "replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1250", "replace")  # common for Central European stations


STREAMTITLE_RE = re.compile(r"StreamTitle='(.*?)';(?=Stream|\s*$)", re.S)


def watch_stream(url, rec, encoding):
    backoff = 2
    while True:
        sock = None
        try:
            f, sock, headers, final_url = open_stream(url)
            metaint = int(headers.get("icy-metaint", "0") or 0)
            if not metaint:
                raise SystemExit(
                    "This stream does not send song metadata (no icy-metaint header).\n"
                    "Try --system mode instead."
                )
            station = headers.get("icy-name") or urlsplit(final_url).hostname
            print(f"Connected to: {station}   (Ctrl+C to stop)", flush=True)
            rec.header()
            backoff = 2
            while True:
                read_exact(f, metaint)                 # skip audio
                length = read_exact(f, 1)[0] * 16
                if not length:
                    continue                           # no change this block
                meta = decode_meta(read_exact(f, length).rstrip(b"\0"), encoding)
                m = STREAMTITLE_RE.search(meta)
                if m:
                    raw = m.group(1).strip()
                    artist, title = split_artist_title(raw)
                    rec.record(artist, title, raw, station)
        except OSError as e:  # includes ConnectionError and timeouts
            print(f"Connection problem ({e}); retrying in {backoff}s…", file=sys.stderr, flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
        finally:
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass


# --------------------------------------------------------------------------- system mode

def make_windows_reader():
    try:
        from winrt.windows.media.control import \
            GlobalSystemMediaTransportControlsSessionManager as Manager
    except ImportError:
        try:
            from winsdk.windows.media.control import \
                GlobalSystemMediaTransportControlsSessionManager as Manager
        except ImportError:
            raise SystemExit(
                "System mode on Windows needs the WinRT bindings:\n"
                "  pip install winrt-runtime winrt-Windows.Media.Control winrt-Windows.Foundation winrt-Windows.Foundation.Collections winrt-Windows.Storage.Streams"
            )
    import asyncio

    async def query(player):
        mgr = await Manager.request_async()
        sessions = list(mgr.get_sessions())
        if player:
            sessions = [s for s in sessions if player.lower() in s.source_app_user_model_id.lower()]
        else:
            current = mgr.get_current_session()
            sessions = [current] if current else []
        if not sessions:
            return None
        s = sessions[0]
        props = await s.try_get_media_properties_async()
        return props.artist, props.title, s.source_app_user_model_id

    return lambda player: asyncio.run(query(player))


def make_linux_reader():
    if not shutil.which("playerctl"):
        raise SystemExit("System mode on Linux needs playerctl (e.g. sudo apt install playerctl).")

    def read(player):
        cmd = ["playerctl"] + (["-p", player] if player else []) + \
              ["metadata", "--format", "{{artist}}\t{{title}}\t{{playerName}}"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return None
        parts = (r.stdout.rstrip("\n").split("\t") + ["", "", ""])[:3]
        return tuple(parts)

    return read


def watch_system(rec, interval, player):
    system = platform.system()
    if system == "Windows":
        reader = make_windows_reader()
    elif system == "Linux":
        reader = make_linux_reader()
    else:
        raise SystemExit(f"System mode isn't supported on {system}; use --url (stream mode).")

    print(f"Watching the system media session every {interval}s   (Ctrl+C to stop)", flush=True)
    rec.header()
    while True:
        try:
            info = reader(player)
            if info:
                artist, title, app = info
                raw = f"{artist} - {title}" if artist else title
                if not artist:  # many radio players put "Artist - Title" into the title
                    artist, title = split_artist_title(title)
                rec.record(artist, title, raw, app)
        except ImportError as e:
            raise SystemExit(
                f"A Windows component is missing ({e.name}). Re-run install.bat, or:\n"
                f"  py -m pip install --user {WIN_PKGS}"
            )
        except Exception as e:  # keep running through transient player hiccups
            print(f"Could not read media info: {e}", file=sys.stderr, flush=True)
        time.sleep(interval)


# --------------------------------------------------------------------------- show

def parse_when(text):
    """'3h', '30m', '2d', 'today', 'YYYY-MM-DD', 'YYYY-MM-DD HH:MM' -> datetime"""
    text = text.strip().lower()
    m = re.fullmatch(r"(\d+)\s*([mhdw])", text)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"m": timedelta(minutes=n), "h": timedelta(hours=n),
                 "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]
        return datetime.now() - delta
    if text == "today":
        return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    if text == "yesterday":
        return (datetime.now() - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%H:%M"):
        try:
            dt = datetime.strptime(text, fmt)
            if fmt == "%H:%M":
                dt = datetime.combine(datetime.now().date(), dt.time())
            return dt
        except ValueError:
            pass
    raise SystemExit(f"Don't understand the time '{text}'. Use e.g. 3h, today, 2026-10-18 20:00")


def cmd_show(args):
    con = db_connect()
    sql = "SELECT played_at, artist, title, source FROM plays WHERE 1=1"
    params = []
    since = parse_when("today") if args.today else (parse_when(args.since) if args.since else None)
    if since:
        sql += " AND played_at >= ?"; params.append(since.isoformat(timespec="seconds"))
    if args.until:
        sql += " AND played_at <= ?"; params.append(parse_when(args.until).isoformat(timespec="seconds"))
    if args.search:
        sql += " AND (artist LIKE ? OR title LIKE ?)"; params += [f"%{args.search}%"] * 2
    sql += " ORDER BY played_at DESC"
    if not (since or args.until) or args.limit_given:
        sql += " LIMIT ?"; params.append(args.limit)
    rows = list(reversed(con.execute(sql, params).fetchall()))

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["played_at", "artist", "title", "source"])
            w.writerows(rows)
        print(f"Wrote {len(rows)} songs to {args.csv}")
        return

    if not rows:
        print("Nothing logged for that selection yet.")
        return

    width = shutil.get_terminal_size((100, 20)).columns
    print()
    for played_at, artist, title, source in rows:
        when = played_at.replace("T", " ")          # 2026-10-19 01:21:05
        line = f"  {when}  {artist or '?'} — {title}"
        if not args.no_source and source:
            line += f"   ({source})"
        if len(line) > width:
            line = line[: width - 1] + "…"
        print(line)
    print(f"\n{len(rows)} song(s)")


# --------------------------------------------------------------------------- config

def cmd_config(args):
    if args.reset:
        try:
            config_file().unlink()
        except FileNotFoundError:
            pass
        print("Setting cleared. radiolog will ask where to store the database next time it starts.\n"
              "(Your existing history file was not touched.)")
        return

    current = Path(load_config().get("db_path") or DEFAULT_DB)
    if args.db:
        new = normalize_db_input(args.db)
        ok, err = check_writable(new)
        if not ok:
            need, how = admin_words()
            print(f"✖ radiolog can't write to {new.parent} ({err.strerror or err}).\n"
                  f"  This location needs {need}; you'd have to {how} every time.")
            if ask("Use it anyway? [y/N]: ", "n").lower() not in ("y", "yes", "t", "tak"):
                print("Nothing changed.")
                return
        offer_move(current, new)
        save_config({"db_path": str(new)})
        print(f"✔ Song history location set to:\n  {new}")
        return

    need, how = admin_words()
    ok, _ = check_writable(current)
    print(f"Database:      {current}")
    print("Songs logged:  " + (str(count_songs(current)) if current.exists() else "0 (file not created yet)"))
    print("Writable:      " + ("yes" if ok else f"NO (needs {need}; {how})"))
    print(f"Settings file: {config_file()}")
    if os.environ.get("RADIOLOG_DB"):
        print(f"Note: RADIOLOG_DB is set, so {os.environ['RADIOLOG_DB']} is used instead.")


# --------------------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(description="Log and browse what your internet radio played.")
    sub = p.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("watch", help="record songs as they play")
    mode = w.add_mutually_exclusive_group(required=True)
    mode.add_argument("--url", help="radio stream URL (direct stream, .pls or .m3u)")
    mode.add_argument("--system", action="store_true",
                      help="read 'now playing' from the OS media session (Windows/Linux)")
    w.add_argument("--player", help="system mode: only follow this player (e.g. vlc, spotify, chrome)")
    w.add_argument("--interval", type=float, default=5, help="system mode: seconds between checks (default 5)")
    w.add_argument("--encoding", default="auto",
                   help="stream mode: metadata text encoding (default auto = utf-8, then cp1250)")

    s = sub.add_parser("show", help="display the logged songs")
    s.add_argument("--today", action="store_true", help="only songs from today")
    s.add_argument("--since", help="e.g. 3h, 2d, yesterday, 20:00, '2026-10-18 20:00'")
    s.add_argument("--until", help="same formats as --since")
    s.add_argument("--search", help="filter by artist or title text")
    s.add_argument("-n", "--limit", type=int, default=None, help="max songs (default 50 without a time filter)")
    s.add_argument("--csv", metavar="FILE", help="export the selection to a CSV file instead of printing")
    s.add_argument("--no-source", action="store_true", help="hide the station/player column")

    c = sub.add_parser("config", help="show or change where the song history is stored")
    c.add_argument("--db", metavar="LOCATION", help="new folder or file for the database (history is moved)")
    c.add_argument("--reset", action="store_true", help="forget the setting; ask again on next start")

    args = p.parse_args()

    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except AttributeError:
            pass

    global DB_PATH
    try:
        if args.cmd == "config":
            cmd_config(args)
            return
        DB_PATH = resolve_db_path()
        if args.cmd == "watch":
            rec = Recorder(db_connect())
            print(f"Saving to {DB_PATH}")
            if args.url:
                watch_stream(args.url, rec, args.encoding)
            else:
                watch_system(rec, args.interval, args.player)
        else:
            args.limit_given = args.limit is not None
            if args.limit is None:
                args.limit = 50
            cmd_show(args)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
