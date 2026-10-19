#!/usr/bin/env python3
"""
radiolog - logs every song played on your PC, with the date and time it was heard.
Made for internet radio; works with any player that reports "now playing".

First start
    The first time you run any command, radiolog asks two questions
    (press Enter for the default):
      1. where to store your song history   (default: ~/.radiolog.db, i.e. your user folder)
      2. where to save downloaded songs     (default: ~/Music/radiolog)
    Choose locations you can write to without administrator rights. If you choose
    one that needs them (C:\\Program Files, C:\\Windows, /usr, ...), radiolog must be
    run elevated (Run as administrator / sudo) every time it uses that location.

Record
    radiolog watch --system              follow your media player (Windows media session / Linux MPRIS)
    radiolog watch --url STREAM_URL      read song titles straight from a Shoutcast/Icecast stream

Browse
    radiolog show                        last 50 songs, each with a number (#12)
    radiolog show --today | --since 3h | --since "2026-10-18 20:00" --until 23:00
    radiolog show --search queen
    radiolog show --today --csv today.csv
    radiolog show --sort artist --unique each song once, A-Z by artist, with play counts
    radiolog show --sort genre           grouped by genre (looked up online once per song)
    radiolog show --genre rock           only rock songs

Search and download (song numbers come from 'show'; no number = last song)
    radiolog pick                        choose songs from a list (several: 3 5 8-12 or all),
                                         then search or download them
    radiolog search 12 [--ytmusic | --spotify]
    radiolog download 12 [--video]       best quality via yt-dlp
    radiolog download 12 --format mp3    audio as best (default) | opus | m4a | mp3, this time only
    radiolog download 3 5 8-12           batch: several numbers and ranges
    radiolog download --today            batch: everything heard today

Clear the history (asks first, saves a backup; downloads are not touched)
    radiolog clear                       delete everything
    radiolog clear 3 5 8-12              delete chosen songs
    radiolog clear --until 30d           delete songs older than 30 days

Settings
    radiolog config                      show where the history and downloads are
    radiolog config --db LOCATION        move the song history (folder or file path)
    radiolog config --downloads FOLDER   change the download folder
    radiolog config --format mp3         download audio as best | opus | m4a | mp3
    radiolog config --reset              forget all settings; setup runs again on next start
    Settings live in %APPDATA%\\radiolog\\config.json (Linux: ~/.config/radiolog/).
    The RADIOLOG_DB environment variable overrides the history location for one run.

Update
    radiolog update                      install the newest release from GitHub (shows what's new)
    radiolog update --check              only check
    radiolog --version                   show the installed version

Run  radiolog COMMAND --help  for all options of a command.
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
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, quote_plus, urljoin, urlsplit

IS_WIN = sys.platform == "win32"
DEFAULT_DB = Path.home() / ".radiolog.db"   # default: in the user folder (no admin rights needed)
DB_PATH = None                              # set by resolve_db_path() at startup
GITHUB_REPO = "GamerTeen69/radiolog"


def installed_version():
    try:
        from importlib.metadata import version, PackageNotFoundError
        try:
            return version("radiolog")
        except PackageNotFoundError:
            return None
    except ImportError:
        return None


USER_AGENT = f"radiolog/{installed_version() or 'dev'}"
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


def save_config(changes):
    cfg = load_config()
    cfg.update(changes)
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


DEFAULT_DOWNLOADS = Path.home() / "Music" / "radiolog"


def normalize_folder_input(text):
    text = text.strip().strip('"').strip("'").strip()
    return Path(os.path.expandvars(os.path.expanduser(text))).resolve()


def choose_download_location(default):
    """Interactive: ask where downloaded songs go. Returns (folder, needs_elevation)."""
    need, how = admin_words()
    example = "D:\\Music" if IS_WIN else "~/Music"
    print(
        "Where should downloaded songs be saved?\n\n"
        f"  Choose a folder you can write to WITHOUT {need}, e.g. your Music folder\n"
        f"  or another drive ({example}). The folder is created if it doesn't exist.\n"
        "  Press Enter for the default (Music\\radiolog in your user folder).\n"
        if IS_WIN else
        "Where should downloaded songs be saved?\n\n"
        f"  Choose a folder you can write to WITHOUT {need}, e.g. {example}.\n"
        "  The folder is created if it doesn't exist.\n"
        "  Press Enter for the default (~/Music/radiolog).\n"
    )
    while True:
        folder = normalize_folder_input(ask(f"Download folder [{default}]: ", str(default)))
        ok, err = check_writable(folder / "radiolog-probe")
        if ok:
            return folder, False
        print(f"\n✖ radiolog can't write to {folder}\n  ({err.strerror or err})\n"
              f"  This folder needs {need}.\n\n"
              "  [1] choose a different folder (recommended)\n"
              f"  [2] keep it: I'll {how} every time I download\n")
        if ask("Your choice [1]: ", "1") == "2":
            return folder, True
        print()


def ask_download_location():
    """Ask once and save. Returns the chosen folder."""
    folder, needs_elevation = choose_download_location(DEFAULT_DOWNLOADS)
    save_config({"download_dir": str(folder)})
    print(f"\n✔ Downloads will be saved in:\n  {folder}\n"
          "  (change it any time with:  radiolog config --downloads FOLDER)")
    if needs_elevation:
        _, how = admin_words()
        print(f"  This folder needs elevated rights, so {how} when downloading.")
    return folder


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
    print("Step 1 of 2: song history\n")
    db_path, needs_elevation = choose_db_location(default)
    offer_move(DEFAULT_DB, db_path)
    save_config({"db_path": str(db_path)})
    print(f"\n✔ Your song history will be stored in:\n  {db_path}\n"
          f"  (change it any time with:  radiolog config --db NEW_LOCATION)\n")

    print("Step 2 of 2: downloads\n")
    ask_download_location()
    print()
    if needs_elevation:
        _, how = admin_words()
        raise SystemExit(f"Your song history location needs elevated rights, so {how} from now on.")
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
    con.execute(
        """CREATE TABLE IF NOT EXISTS genres (
               song_key   TEXT PRIMARY KEY,   -- lower(artist) + '\x1f' + lower(title)
               genre      TEXT,               -- '' = looked up, nothing found
               source     TEXT,               -- deezer / itunes
               checked_at TEXT
           )"""
    )
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


def query_rows(args, default_limit=50):
    """Songs matching the show/pick filters, oldest first: (id, played_at, artist, title, source)."""
    con = db_connect()
    sql = "SELECT id, played_at, artist, title, source FROM plays WHERE 1=1"
    params = []
    since = parse_when("today") if args.today else (parse_when(args.since) if args.since else None)
    if since:
        sql += " AND played_at >= ?"; params.append(since.isoformat(timespec="seconds"))
    if args.until:
        sql += " AND played_at <= ?"; params.append(parse_when(args.until).isoformat(timespec="seconds"))
    if args.search:
        sql += " AND (artist LIKE ? OR title LIKE ?)"; params += [f"%{args.search}%"] * 2
    sql += " ORDER BY played_at DESC, id DESC"
    limit = args.limit if args.limit is not None else (None if (since or args.until) else default_limit)
    if limit is not None:
        sql += " LIMIT ?"; params.append(limit)
    return list(reversed(con.execute(sql, params).fetchall()))


def print_rows(rows, show_source=True, extra=None, blank=True):
    width = shutil.get_terminal_size((100, 20)).columns
    num_w = len(str(max(r[0] for r in rows)))
    if blank:
        print()
    for song_id, played_at, artist, title, source in rows:
        when = played_at.replace("T", " ")          # 2026-10-19 01:21:05
        line = f"  #{song_id:<{num_w}}  {when}  {artist or '?'} — {title}"
        if extra and song_id in extra:
            line += extra[song_id]
        if show_source and source:
            line += f"   ({source})"
        if len(line) > width:
            line = line[: width - 1] + "…"
        print(line)


# --------------------------------------------------------------------------- genres

def song_key(artist, title):
    return f"{(artist or '').strip().lower()}\x1f{(title or '').strip().lower()}"


def _get_json(url, timeout=10):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


class GenreLookup:
    """Finds a song's genre in free music databases (Deezer, then iTunes). No account needed."""

    def __init__(self):
        self.album_genre = {}          # Deezer album id -> genre (albums repeat a lot)
        self._last_deezer = 0.0
        self._last_itunes = 0.0

    def _wait(self, attr, gap):
        delay = getattr(self, attr) + gap - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        setattr(self, attr, time.monotonic())

    def deezer(self, artist, title):
        queries = []
        if artist:
            queries.append(f'artist:"{artist}" track:"{title}"')
        queries.append(f"{artist} {title}".strip())
        for q in queries:
            self._wait("_last_deezer", 0.12)              # Deezer allows 50 requests / 5 s
            res = _get_json("https://api.deezer.com/search?limit=1&q=" + quote(q))
            if res.get("error"):
                if "quota" in str(res["error"]).lower():
                    time.sleep(5)
                continue
            data = res.get("data") or []
            if not data:
                continue
            album_id = (data[0].get("album") or {}).get("id")
            if not album_id:
                continue
            if album_id not in self.album_genre:
                self._wait("_last_deezer", 0.12)
                album = _get_json(f"https://api.deezer.com/album/{album_id}")
                names = [g.get("name") for g in ((album.get("genres") or {}).get("data") or []) if g.get("name")]
                self.album_genre[album_id] = names[0] if names else ""
            if self.album_genre[album_id]:
                return self.album_genre[album_id]
        return ""

    def itunes(self, artist, title):
        self._wait("_last_itunes", 3.1)                   # iTunes allows ~20 requests / minute
        res = _get_json("https://itunes.apple.com/search?entity=song&limit=1&term=" +
                        quote_plus(f"{artist} {title}".strip()))
        results = res.get("results") or []
        return (results[0].get("primaryGenreName") or "") if results else ""

    def find(self, artist, title):
        """(genre, source). Raises OSError when offline."""
        g = self.deezer(artist, title)
        if g:
            return g, "deezer"
        g = self.itunes(artist, title)
        return (g, "itunes") if g else ("", "")


def genres_for(rows, refresh=False):
    """{song id: genre} for the given rows, looking up songs not seen before.
    Results are remembered in the database, so each song is looked up only once."""
    con = db_connect()
    keys = {r[0]: song_key(r[2], r[3]) for r in rows}
    cache = {}
    unique = list(dict.fromkeys(keys.values()))
    for i in range(0, len(unique), 500):
        part = unique[i:i + 500]
        for k, g, checked in con.execute(
                f"SELECT song_key, genre, checked_at FROM genres WHERE song_key IN ({','.join('?' * len(part))})", part):
            # songs where nothing was found are retried after 30 days
            stale = not g and (not checked or datetime.now() - datetime.fromisoformat(checked) > timedelta(days=30))
            if not refresh and not stale:
                cache[k] = g

    todo = []
    for r in rows:
        k = keys[r[0]]
        if k not in cache and k not in {song_key(a, b) for a, b in todo}:
            todo.append((r[2], r[3]))
    if todo:
        lookup = GenreLookup()
        print(f"Looking up genres for {len(todo)} song(s) (first time only)… Ctrl+C to skip the rest.",
              flush=True)
        done = 0
        try:
            for artist, title in todo:
                try:
                    genre, source = lookup.find(artist, title)
                except (OSError, ValueError) as e:
                    print(f"\nCouldn't reach the music databases ({e}). Showing the genres found so far.")
                    break
                k = song_key(artist, title)
                cache[k] = genre
                con.execute("INSERT OR REPLACE INTO genres (song_key, genre, source, checked_at) VALUES (?,?,?,?)",
                            (k, genre, source, datetime.now().isoformat(timespec="seconds")))
                con.commit()
                done += 1
                if sys.stdout.isatty():
                    print(f"\r  {done}/{len(todo)}  {(artist + ' — ' + title)[:50]:50}", end="", flush=True)
        except KeyboardInterrupt:
            print("\n(skipped the rest; they'll be looked up next time)")
        if sys.stdout.isatty():
            print("\r" + " " * 70 + "\r", end="")
    return {rid: cache.get(k, "") for rid, k in keys.items()}


def sort_key_text(s):
    """Case-insensitive, ignores a leading 'The ' (The Hives sorts under H)."""
    s = (s or "").strip().lower()
    return s[4:] if s.startswith("the ") else s


def cmd_show(args):
    sorting = args.sort != "time" or args.genre or args.unique
    if sorting and args.limit is None and not (args.today or args.since or args.until):
        args.limit = 10 ** 9            # sorted/grouped views cover the whole history by default
    rows = query_rows(args)

    genres = {}
    if (args.sort == "genre" or args.genre or args.with_genre) and rows:
        genres = genres_for(rows, refresh=args.refresh_genres)
        if args.genre:
            want = args.genre.lower()
            rows = [r for r in rows if want in (genres.get(r[0]) or "").lower()]

    counts = {}
    if args.unique:                     # one line per song, newest play, with a play count
        latest = {}
        for r in rows:
            k = song_key(r[2], r[3])
            counts[k] = counts.get(k, 0) + 1
            latest[k] = r
        rows = sorted(latest.values(), key=lambda r: r[1])

    if args.sort == "artist":
        rows.sort(key=lambda r: (sort_key_text(r[2]), sort_key_text(r[3]), r[1]))
    elif args.sort == "title":
        rows.sort(key=lambda r: (sort_key_text(r[3]), sort_key_text(r[2]), r[1]))
    elif args.sort == "genre":
        rows.sort(key=lambda r: (genres.get(r[0]) == "", (genres.get(r[0]) or "").lower(),
                                 sort_key_text(r[2]), sort_key_text(r[3]), r[1]))

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            header = ["id", "played_at", "artist", "title", "source"]
            if genres:
                header.append("genre")
            if counts:
                header.append("plays")
            w.writerow(header)
            for r in rows:
                line = list(r)
                if genres:
                    line.append(genres.get(r[0], ""))
                if counts:
                    line.append(counts[song_key(r[2], r[3])])
                w.writerow(line)
        print(f"Wrote {len(rows)} songs to {args.csv}")
        return

    if not rows:
        print("Nothing logged for that selection yet." if not args.genre
              else f"No songs with a genre matching '{args.genre}'.")
        return

    extra = {}
    for r in rows:
        bits = []
        if counts and counts[song_key(r[2], r[3])] > 1:
            bits.append(f"×{counts[song_key(r[2], r[3])]}")
        if genres and args.sort != "genre" and genres.get(r[0]):
            bits.append(f"[{genres[r[0]]}]")
        if bits:
            extra[r[0]] = "  " + " ".join(bits)

    if args.sort == "genre":
        groups = {}
        for r in rows:
            groups.setdefault(genres.get(r[0]) or "", []).append(r)
        for g, members in groups.items():
            label = g or "Unknown genre"
            print(f"\n── {label} ({len(members)}) " + "─" * max(2, 40 - len(label)))
            print_rows(members, not args.no_source, extra=extra, blank=False)
    else:
        print_rows(rows, not args.no_source, extra=extra)
    what = "song(s)" if not args.unique else "different song(s)"
    print(f"\n{len(rows)} {what}   ·   search/download one:  radiolog pick   or   radiolog download NUMBER")


# --------------------------------------------------------------------------- search & download

def get_song(ref):
    """'last' or a song number (with or without #) -> (id, artist, title)."""
    con = db_connect()
    ref = (ref or "last").strip().lstrip("#").lower()
    if ref == "last":
        row = con.execute("SELECT id, artist, title FROM plays ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            raise SystemExit("Nothing logged yet.")
        return row
    if not ref.isdigit():
        raise SystemExit(f"'{ref}' is not a song number. Use the # number from 'radiolog show', or 'last'.")
    row = con.execute("SELECT id, artist, title FROM plays WHERE id = ?", (int(ref),)).fetchone()
    if not row:
        raise SystemExit(f"There's no song #{ref}. See the numbers with:  radiolog show")
    return row


def song_query(artist, title):
    return f"{artist} - {title}" if artist else title


def search_url(service, artist, title):
    q = song_query(artist, title)
    if service == "spotify":
        return "https://open.spotify.com/search/" + quote(q, safe="")
    if service == "ytmusic":
        return "https://music.youtube.com/search?q=" + quote_plus(q)
    return "https://www.youtube.com/results?search_query=" + quote_plus(q)


SERVICE_NAMES = {"youtube": "YouTube", "ytmusic": "YouTube Music", "spotify": "Spotify"}


def open_search(service, artist, title):
    url = search_url(service, artist, title)
    print(f"Opening {SERVICE_NAMES[service]}: {song_query(artist, title)}")
    if not webbrowser.open(url):
        print(f"Couldn't open a browser. Here's the link:\n  {url}")


def default_download_dir():
    saved = load_config().get("download_dir")
    return Path(saved) if saved else DEFAULT_DOWNLOADS


def download_dir(folder=None, ask_if_unset=True):
    """--dir if given, else the saved folder; asks once if it was never chosen."""
    if folder:
        return Path(folder).expanduser()
    if ask_if_unset and not load_config().get("download_dir") and sys.stdin.isatty():
        print("\n── First download: choose a folder ─────────────────────\n")
        chosen = ask_download_location()
        print("─" * 56 + "\n")
        return chosen
    return default_download_dir()


def safe_filename(text):
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text).strip(" .")
    return text[:150] or "song"


def ytdlp_base():
    """How to start yt-dlp: the Python module installed with radiolog, else a yt-dlp on PATH."""
    try:
        import yt_dlp  # noqa: F401
        return [sys.executable, "-m", "yt_dlp"]
    except ImportError:
        exe = shutil.which("yt-dlp")
        if exe:
            return [exe]
    return None


AUDIO_FORMATS = {
    "best": "the original YouTube stream, no conversion (usually .opus, best quality)",
    "opus": ".opus, YouTube's Opus stream as-is (same as best in most cases)",
    "m4a":  ".m4a (AAC), YouTube's AAC stream as-is; plays on Apple devices",
    "mp3":  ".mp3, converted at the highest MP3 quality; plays everywhere, slightly lower quality",
}
FORMAT_OVERRIDE = None          # set by 'download --format' for one run


def audio_format():
    fmt = FORMAT_OVERRIDE or load_config().get("audio_format") or "best"
    return fmt if fmt in AUDIO_FORMATS else "best"


MEDIA_EXTS = {".opus", ".webm", ".ogg", ".oga", ".m4a", ".mp4", ".aac", ".mp3", ".flac", ".wav",
              ".mka", ".mkv", ".mov", ".3gp"}
THUMB_EXTS = {".webp", ".png", ".jpg", ".jpeg"}


def format_exts(fmt, video=False):
    """File extensions a finished download in this format can have."""
    if video or fmt == "best":
        return MEDIA_EXTS
    return {"opus": {".opus", ".webm", ".ogg"}, "m4a": {".m4a"}, "mp3": {".mp3"}}[fmt]


def search_queries(artist, title):
    """What to search YouTube for: 'Artist - Title', then a plainer version without punctuation
    (apostrophes, dashes, brackets… sometimes make YouTube's search return nothing)."""
    first = song_query(artist, title).replace('"', "")
    plain = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", f"{artist} {title}")).strip()
    return [first] + ([plain] if plain and plain.lower() != first.lower() else [])


def build_ytdlp_cmd(artist, title, folder, video=False, have_ffmpeg=True, fmt=None, query=None,
                    force=False):
    """yt-dlp command that finds the song on YouTube and downloads the best quality available."""
    fmt = fmt or audio_format()
    name = output_name(artist, title, video)
    cmd = ["--no-playlist", "--paths", str(folder), "-o", f"{name}.%(ext)s"]
    if video and have_ffmpeg:
        # best video + best audio streams, merged into .mkv (holds any codec, no re-encoding)
        cmd += ["-f", "bv*+ba/b", "--merge-output-format", "mkv"]
    elif video:
        cmd += ["-f", "b"]          # best file that already has video and audio together
    elif fmt == "mp3" and have_ffmpeg:
        # YouTube has no MP3: take the best stream and convert at the top MP3 quality (VBR V0)
        cmd += ["-f", "ba/b", "-x", "--audio-format", "mp3", "--audio-quality", "0"]
    elif fmt == "m4a":
        # prefer YouTube's own AAC stream so nothing is re-encoded
        cmd += ["-f", "ba[ext=m4a]/ba/b"]
        if have_ffmpeg:
            cmd += ["-x", "--audio-format", "m4a", "--audio-quality", "0"]
    elif fmt == "opus":
        cmd += ["-f", "ba[acodec=opus]/ba/b"]
        if have_ffmpeg:
            cmd += ["-x", "--audio-format", "opus", "--audio-quality", "0"]
    elif have_ffmpeg:
        # best audio stream, extracted WITHOUT re-encoding (keeps the original quality: opus/m4a)
        cmd += ["-f", "ba/b", "-x", "--audio-quality", "0"]
    else:
        cmd += ["-f", "ba/b"]       # (also mp3 without ffmpeg: can't convert, keep the original)
    if have_ffmpeg:
        cmd += ["--embed-metadata", "--embed-thumbnail"]
    if force:
        cmd.append("--force-overwrites")
    cmd.append("ytsearch1:" + (query or search_queries(artist, title)[0]))
    return cmd


def show_cmd(cmd):
    """The command as you could paste it into a terminal (cmd/PowerShell on Windows, sh elsewhere)."""
    if IS_WIN:
        return subprocess.list2cmdline(cmd)
    import shlex
    return " ".join(shlex.quote(c) for c in cmd)


def output_name(artist, title, video=False):
    return safe_filename(song_query(artist, title) + (" (video)" if video else ""))


def already_downloaded(folder, artist, title, video):
    """Finished file for this song in the folder, in the chosen format (ignores unfinished .part files)."""
    name = output_name(artist, title, video)
    wanted = format_exts(audio_format(), video)
    if not folder.is_dir():
        return None
    for f in folder.iterdir():
        if f.is_file() and f.stem == name and f.suffix.lower() in wanted:
            return f
    return None


def remove_leftover_thumbnails(folder, artist, title, video):
    """yt-dlp leaves the cover image behind when it can't embed it; tidy those up."""
    name = output_name(artist, title, video)
    for ext in THUMB_EXTS:
        f = folder / f"{name}{ext}"
        try:
            if f.is_file():
                f.unlink()
        except OSError:
            pass


YTDLP_MISSING = ("yt-dlp is not installed. Re-run the radiolog installer, or:\n"
                 "  py -m pip install --user -U \"yt-dlp[default]\"" if IS_WIN else
                 "yt-dlp is not installed. Re-run ./install.sh, or:  pipx install yt-dlp")


def ffmpeg_note(video):
    print("Note: ffmpeg was not found, so files are saved exactly as YouTube streams them\n"
          "      (still the best quality, but without cover art/tags" +
          (", and video without merging)." if video else ")."))
    if not video and audio_format() == "mp3":
        print("      MP3 needs ffmpeg for converting, so you'll get the original format instead.")
    print("      Install it for the full result:  " +
          ("winget install Gyan.FFmpeg" if IS_WIN else "sudo apt install ffmpeg") + "\n")


def download_song(artist, title, folder=None, video=False, print_only=False, quiet_header=False,
                  force=False):
    """Download one song. Returns 'ok', 'skipped', 'notfound', 'failed' or 'missing'
    (yt-dlp not installed)."""
    folder = download_dir(folder, ask_if_unset=not print_only)
    have_ffmpeg = bool(shutil.which("ffmpeg"))
    queries = search_queries(artist, title)
    cmd = build_ytdlp_cmd(artist, title, folder, video, have_ffmpeg, query=queries[0], force=force)
    base = ytdlp_base()

    if print_only:
        print(show_cmd(["yt-dlp"] + cmd))
        return "ok"
    if not base:
        print(YTDLP_MISSING)
        print("\nThe command it would run:\n  " + show_cmd(["yt-dlp"] + cmd))
        return "missing"
    existing = None if force else already_downloaded(folder, artist, title, video)
    if existing:
        remove_leftover_thumbnails(folder, artist, title, video)
        print(f"↷ Already downloaded: {existing.name}  (skipped)")
        return "skipped"
    if not have_ffmpeg and not quiet_header:
        ffmpeg_note(video)

    folder.mkdir(parents=True, exist_ok=True)
    kind = "video" if video else ("audio" if audio_format() == "best" else audio_format())
    print(f"Downloading: {song_query(artist, title)}   ({kind}, best quality)")
    if not quiet_header:
        print(f"Into:        {folder}")

    code = 0
    for n, query in enumerate(queries):
        cmd = build_ytdlp_cmd(artist, title, folder, video, have_ffmpeg, query=query, force=force)
        if n:
            print(f"\nNothing found for that. Trying a simpler search: {query}")
        if not quiet_header or n:
            print(f"Command:     {show_cmd(['yt-dlp'] + cmd)}\n")
        code = subprocess.call(base + cmd)
        saved = already_downloaded(folder, artist, title, video)
        if saved:
            remove_leftover_thumbnails(folder, artist, title, video)
            if code != 0:
                print("\n⚠ The song was saved, but yt-dlp reported a problem afterwards "
                      "(usually the cover art or tags).")
            if not quiet_header:
                print(f"\n✔ Done. Saved as: {saved}")
            return "ok"
        if code != 0:
            break                       # a real error, not an empty search: don't retry

    if code == 0:
        print(f"\n✖ Not found on YouTube: {song_query(artist, title)}\n"
              "  Nothing was downloaded. Check the song with:  radiolog search NUMBER")
        return "notfound"
    if not quiet_header:
        print(f"\n✖ yt-dlp stopped with an error (code {code}). If YouTube changed something,\n"
              "  updating usually fixes it: re-run the installer, or\n"
              "  py -m pip install --user -U \"yt-dlp[default]\"")
    return "failed"


def unique_songs(songs):
    """Drop repeats of the same artist+title (radio plays hits many times)."""
    seen, out = set(), []
    for s in songs:
        key = ((s[1] or "").lower(), s[2].lower())
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out


def parse_song_refs(refs, available=None):
    """'3 5,7 10-15 last' -> list of song numbers (ranges limited to what exists)."""
    ids = []
    for token in re.split(r"[\s,]+", " ".join(refs).strip()):
        token = token.lstrip("#").lower()
        if not token:
            continue
        m = re.fullmatch(r"(\d+)-#?(\d+)", token)
        if m:
            a, b = sorted((int(m.group(1)), int(m.group(2))))
            ids += [i for i in range(a, b + 1) if available is None or i in available]
        elif token.isdigit() or token == "last":
            ids.append(token)
        else:
            raise SystemExit(f"'{token}' is not a song number or range (examples: 12, 3-8, last).")
    return ids


def songs_from_ids(ids):
    """Look up song numbers; numbers from a range that don't exist are skipped silently."""
    con = db_connect()
    out = []
    for i in ids:
        if i == "last":
            out.append(get_song("last"))
            continue
        row = con.execute("SELECT id, artist, title FROM plays WHERE id = ?", (int(i),)).fetchone()
        if row:
            out.append(row)
        else:
            print(f"(no song #{i}, skipped)")
    return out


def batch_download(songs, folder=None, video=False, print_only=False, confirm=True, force=False):
    songs = unique_songs(songs)
    if not songs:
        print("No songs to download.")
        return
    if len(songs) == 1:
        _, artist, title = songs[0]
        download_song(artist, title, folder, video, print_only, force=force)
        return

    folder = download_dir(folder, ask_if_unset=not print_only)
    if print_only:
        for _, artist, title in songs:
            download_song(artist, title, folder, video, print_only=True)
        return
    if not ytdlp_base():
        print(YTDLP_MISSING)
        return

    kind = "video" if video else ("audio" if audio_format() == "best" else audio_format())
    print(f"\nBatch download: {len(songs)} songs ({kind}, best quality)\nInto: {folder}\n")
    for _, artist, title in songs[:10]:
        print(f"  • {song_query(artist, title)}")
    if len(songs) > 10:
        print(f"  … and {len(songs) - 10} more")
    if confirm and sys.stdin.isatty():
        if ask(f"\nDownload all {len(songs)}? [Y/n]: ", "y").lower() not in ("y", "yes", "t", "tak"):
            print("Cancelled.")
            return
    if not shutil.which("ffmpeg"):
        print()
        ffmpeg_note(video)

    results = {"ok": [], "skipped": [], "notfound": [], "failed": []}
    try:
        for n, (_, artist, title) in enumerate(songs, 1):
            print(f"\n[{n}/{len(songs)}] ", end="")
            status = download_song(artist, title, folder, video, quiet_header=True, force=force)
            results[status if status in results else "failed"].append(song_query(artist, title))
    except KeyboardInterrupt:
        print("\n\nStopped. Songs not started yet were not downloaded.")

    print("\n" + "─" * 50)
    print(f"✔ downloaded: {len(results['ok'])}    ↷ already had: {len(results['skipped'])}"
          f"    ? not found: {len(results['notfound'])}    ✖ failed: {len(results['failed'])}")
    for name in results["notfound"]:
        print(f"   ? {name}  (no YouTube result)")
    for name in results["failed"]:
        print(f"   ✖ {name}")
    if results["failed"]:
        print("  Try those again later, or update yt-dlp (re-run the installer).")
    print(f"Saved in: {folder}")


def cmd_search(args):
    _, artist, title = get_song(args.song)
    open_search(args.service, artist, title)


def has_filters(args):
    return any([args.today, args.since, args.until, args.search])


def cmd_download(args):
    global FORMAT_OVERRIDE
    FORMAT_OVERRIDE = args.format
    if args.songs and has_filters(args):
        raise SystemExit("Use either song numbers or filters (--today, --since, --search…), not both.")
    if has_filters(args):
        songs = [(r[0], r[2], r[3]) for r in query_rows(args, default_limit=None)]
    else:
        songs = songs_from_ids(parse_song_refs(args.songs or ["last"]))
    batch_download(songs, args.dir, args.video, args.print_cmd, confirm=not args.yes, force=args.force)


def cmd_pick(args):
    rows = query_rows(args, default_limit=20)
    if not rows:
        print("Nothing logged for that selection yet.")
        return
    print_rows(rows, show_source=False)
    ids = {r[0]: r for r in rows}
    default = str(rows[-1][0])
    print("\nTip: pick several at once, e.g.  3 5 8-12  or  all")
    try:
        while True:
            choice = ask(f"\nSong number(s) [{default}] (q to quit): ", default).strip().lower()
            if choice in ("q", "quit", "exit"):
                return
            try:
                if choice == "all":
                    songs = [(r[0], r[2], r[3]) for r in rows]
                else:
                    songs = songs_from_ids(parse_song_refs([choice], available=set(ids)))
            except SystemExit as e:
                print(f"  {e}")
                continue
            if not songs:
                continue

            if len(songs) == 1:
                song_id, artist, title = songs[0]
                print(f"\n  ♪ {song_query(artist, title)}\n"
                      "  [y] search on YouTube      [m] YouTube Music     [s] Spotify\n"
                      f"  [d] download audio ({audio_format():<4})  [v] download video    [c] show yt-dlp command\n"
                      "  [b] back to the list       [q] quit")
                valid = "ymsdvcbq"
            else:
                songs = unique_songs(songs)
                print(f"\n  ♪ {len(songs)} songs selected")
                for _, artist, title in songs[:5]:
                    print(f"     {song_query(artist, title)}")
                if len(songs) > 5:
                    print(f"     … and {len(songs) - 5} more")
                print(f"  [d] download all ({audio_format()})   [v] download all (video)   [c] show yt-dlp commands\n"
                      "  [b] back to the list       [q] quit")
                valid = "dvcbq"

            while True:
                action = ask("  Action: ", "b").lower()
                if action not in valid:
                    print(f"  Choose one of: {', '.join(valid)}")
                    continue
                if action == "b":
                    break
                if action == "q":
                    return
                _, artist, title = songs[0]
                if action == "y":
                    open_search("youtube", artist, title)
                elif action == "m":
                    open_search("ytmusic", artist, title)
                elif action == "s":
                    open_search("spotify", artist, title)
                elif action in ("d", "v"):
                    batch_download(songs, args.dir, video=(action == "v"), confirm=False)
                elif action == "c":
                    batch_download(songs, args.dir, print_only=True)
    except KeyboardInterrupt:
        print()


# --------------------------------------------------------------------------- clear

def cmd_clear(args):
    """Delete songs from the history: everything, chosen numbers, or a filtered selection."""
    if args.songs and has_filters(args):
        raise SystemExit("Use either song numbers or filters (--today, --since, --until, --search), not both.")
    con = db_connect()

    if args.songs:
        ids = [int(i) for i in parse_song_refs(args.songs) if i != "last"]
        if "last" in parse_song_refs(args.songs):
            ids.append(get_song("last")[0])
        rows = [r for i in sorted(set(ids)) for r in con.execute(
            "SELECT id, played_at, artist, title, source FROM plays WHERE id = ?", (i,))]
        what = "the selected songs"
    elif has_filters(args):
        args.limit = None
        rows = query_rows(args, default_limit=None)
        what = "the songs matching your filters"
    else:
        rows = con.execute("SELECT id, played_at, artist, title, source FROM plays ORDER BY id").fetchall()
        what = "your ENTIRE song history"

    total = con.execute("SELECT COUNT(*) FROM plays").fetchone()[0]
    if not rows:
        print("Nothing to delete: no songs match." if total else "The history is already empty.")
        return

    print(f"\nThis will delete {what}: {len(rows)} of {total} song(s).")
    if len(rows) <= 10:
        print_rows(rows, show_source=False)
    else:
        print_rows(rows[:5], show_source=False)
        print(f"  … {len(rows) - 10} more …")
        print_rows(rows[-5:], show_source=False)
    print(f"\nDatabase: {DB_PATH}")
    print("Downloaded songs and your settings are not affected.")

    if not args.yes:
        if not sys.stdin.isatty():
            raise SystemExit("Not deleting anything: add -y to confirm when not running in a terminal.")
        answer = ask("\nType 'yes' to delete them: ", "").lower()
        if answer not in ("yes", "tak"):
            print("Cancelled. Nothing was deleted.")
            return

    backup = None
    if not args.no_backup:
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        backup = DB_PATH.with_name(f"{DB_PATH.stem}-backup-{stamp}{DB_PATH.suffix}")
        n = 2
        while backup.exists():              # never overwrite an earlier backup
            backup = DB_PATH.with_name(f"{DB_PATH.stem}-backup-{stamp}-{n}{DB_PATH.suffix}")
            n += 1
        try:
            backup_con = sqlite3.connect(backup)
            con.backup(backup_con)          # consistent copy, even while 'watch' is running
            backup_con.close()
        except (sqlite3.Error, OSError) as e:
            raise SystemExit(f"Couldn't save a backup to {backup} ({e}).\n"
                             "Nothing was deleted. Use --no-backup to delete without one.")

    con.executemany("DELETE FROM plays WHERE id = ?", [(r[0],) for r in rows])
    con.commit()
    try:
        con.execute("VACUUM")               # actually shrink the file
    except sqlite3.Error:
        pass
    left = con.execute("SELECT COUNT(*) FROM plays").fetchone()[0]
    print(f"\n✔ Deleted {len(rows)} song(s). {left} left in the history.")
    if backup:
        print(f"  Backup of the previous history: {backup}\n"
              "  (to undo: close radiolog and copy the backup over the database file)")


# --------------------------------------------------------------------------- update

def version_tuple(v):
    """'v1.6.2' -> (1, 6, 2, 0); a 4th number (e.g. 1.6.2.1 / .post1) counts as newer."""
    nums = [int(x) for x in re.findall(r"\d+", v or "")[:4]]
    return tuple(nums + [0] * (4 - len(nums))) if nums else (0, 0, 0, 0)


def latest_release():
    """The newest published release on GitHub: dict with tag_name, name, body, html_url."""
    import urllib.request
    req = urllib.request.Request(
        f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
        headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        raise SystemExit(f"Couldn't reach GitHub to check for updates ({e}).\n"
                         f"Check your internet connection, or see https://github.com/{GITHUB_REPO}/releases")


def install_method():
    """How this copy was installed: 'pipx', 'venv' or 'user'."""
    prefix = sys.prefix.replace("\\", "/").lower()
    if "/pipx/venvs/" in prefix:
        return "pipx"
    if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        return "venv"
    return "user"


def update_command(tag):
    url = f"https://github.com/{GITHUB_REPO}/archive/refs/tags/{tag}.zip"
    method = install_method()
    if method == "pipx":
        return ["pipx", "install", "--force", url]
    spec = f"radiolog[windows] @ {url}" if IS_WIN else f"radiolog @ {url}"
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "--disable-pip-version-check",
           "--no-warn-script-location"]
    if method == "user":
        cmd.append("--user")
    return cmd + [spec]


def run_update_windows(cmd, version):
    """radiolog.exe can't replace itself while running, so the update runs in a new window
    right after this program exits."""
    import tempfile
    bat = Path(tempfile.gettempdir()) / "radiolog-update.bat"
    bat.write_text(
        "@echo off\r\n"
        f"echo Updating radiolog to {version}...\r\n"
        "timeout /t 2 /nobreak >nul\r\n"
        + subprocess.list2cmdline(cmd) + "\r\n"
        "if errorlevel 1 (\r\n"
        "  echo.\r\n"
        "  echo Update failed. See the messages above, or run install.bat from the new release.\r\n"
        ") else (\r\n"
        "  echo.\r\n"
        f"  echo radiolog {version} is installed. Close this window and open a new terminal.\r\n"
        ")\r\n"
        "pause\r\n", encoding="utf-8")
    subprocess.Popen(["cmd", "/c", str(bat)], creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10))
    print("The update continues in a new window. radiolog closes now so it can be replaced.")


def cmd_update(args):
    current = installed_version()
    print("Checking GitHub for updates…")
    rel = latest_release()
    tag = rel.get("tag_name", "")
    latest = tag.lstrip("v")
    print(f"Installed: {current or 'unknown (running from the source folder)'}")
    print(f"Latest:    {latest}   {rel.get('html_url', '')}")

    if current and version_tuple(latest) <= version_tuple(current) and not args.force:
        print("\n✔ You already have the latest version.")
        return
    if not current:
        print("\nThis copy isn't installed with the installer, so it can't update itself.\n"
              "Download the latest release and run install.bat (Windows) or ./install.sh.")
        return

    notes = []
    for line in (rel.get("body") or "").strip().splitlines():
        if line.startswith("## Install"):
            break                       # install steps don't matter when updating
        if line.startswith("```"):
            continue
        line = re.sub(r"^#+\s*", "", line)                          # headings
        line = re.sub(r"\*\*(.+?)\*\*|`(.+?)`", lambda m: m.group(1) or m.group(2), line)
        line = re.sub(r"\[(.+?)\]\((.+?)\)", r"\1 (\2)", line)       # links
        if line.strip() or (notes and notes[-1].strip()):
            notes.append(line)
    while notes and not notes[-1].strip():
        notes.pop()
    if notes:
        print(f"\n── What's new in {latest} " + "─" * 30)
        for line in notes[:25]:
            print("  " + line)
        if len(notes) > 25:
            print(f"  … more at {rel.get('html_url', '')}")
        print("─" * 50)
    if args.check:
        print(f"\nUpdate with:  radiolog update")
        return

    cmd = update_command(tag)
    if not args.yes and sys.stdin.isatty():
        if ask(f"\nUpdate radiolog to {latest} now? [Y/n]: ", "y").lower() not in ("y", "yes", "t", "tak"):
            print("Not updated.")
            return
    print("\nYour song history, downloads and settings are kept.")
    if IS_WIN and install_method() != "pipx":
        run_update_windows(cmd, latest)
        return
    print("Running: " + show_cmd(cmd) + "\n")
    code = subprocess.call(cmd)
    if code == 0:
        print(f"\n✔ radiolog {latest} is installed.")
    else:
        print("\n✖ The update failed (see above). You can always download the release and run the installer.")


# --------------------------------------------------------------------------- config

def cmd_config(args):
    if args.reset:
        try:
            config_file().unlink()
        except FileNotFoundError:
            pass
        print("Settings cleared. radiolog will ask where to store the database next time it starts.\n"
              "(Your existing history file was not touched.)")
        return

    if args.format:
        save_config({"audio_format": args.format})
        print(f"✔ Downloads will be saved as: {args.format}  ({AUDIO_FORMATS[args.format]})")
        if args.format != "best" and not shutil.which("ffmpeg"):
            print("  Note: ffmpeg is needed for " + ("converting to MP3" if args.format == "mp3"
                  else "clean files with cover art") + ". Install it:  " +
                  ("winget install Gyan.FFmpeg" if IS_WIN else "sudo apt install ffmpeg"))
        if not (args.downloads or args.db):
            return

    if args.downloads:
        folder = normalize_folder_input(args.downloads)
        ok, err = check_writable(folder / "radiolog-probe")
        if not ok:
            need, how = admin_words()
            print(f"✖ radiolog can't write to {folder} ({err.strerror or err}).\n"
                  f"  This folder needs {need}; you'd have to {how} when downloading.")
            if ask("Use it anyway? [y/N]: ", "n").lower() not in ("y", "yes", "t", "tak"):
                print("Nothing changed.")
                return
        save_config({"download_dir": str(folder)})
        print(f"✔ Downloads will be saved in:\n  {folder}")
        if not args.db:
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
    print(f"Downloads:     {default_download_dir()}")
    print(f"Audio format:  {audio_format()}  ({AUDIO_FORMATS[audio_format()]})")
    print(f"Settings file: {config_file()}")
    if os.environ.get("RADIOLOG_DB"):
        print(f"Note: RADIOLOG_DB is set, so {os.environ['RADIOLOG_DB']} is used instead.")


# --------------------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(
        prog="radiolog",
        description="Logs every song played on your PC, with the date and time it was heard. "
                    "Made for internet radio; works with any player.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="all options of one command:  radiolog COMMAND --help   (e.g. radiolog config --help)\n"
               "first start asks where to store your history and downloads; see radiolog config --help")
    sub = p.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser(
        "watch", help="record songs as they play", formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Record every song change, with the date and time it was first heard.\n"
            "Leave it running in a terminal; stop with Ctrl+C.\n\n"
            "  --system  reads what your computer reports as 'now playing' (the info in the\n"
            "            Windows volume popup; Linux: MPRIS via playerctl). Works with browser\n"
            "            radio, YouTube, Spotify, VLC and any player that reports the song.\n"
            "  --url     reads song titles straight from a Shoutcast/Icecast radio stream\n"
            "            (direct stream link, .pls or .m3u; not the station's web page)."),
        epilog=(
            "examples:\n"
            "  radiolog watch --system\n"
            "  radiolog watch --system --player chrome\n"
            "  radiolog watch --url http://stream.example.com/radio.mp3"))
    mode = w.add_mutually_exclusive_group(required=True)
    mode.add_argument("--url", help="radio stream URL (direct stream, .pls or .m3u)")
    mode.add_argument("--system", action="store_true",
                      help="read 'now playing' from the OS media session (Windows/Linux)")
    w.add_argument("--player", help="system mode: only follow this player (e.g. vlc, spotify, chrome)")
    w.add_argument("--interval", type=float, default=5, help="system mode: seconds between checks (default 5)")
    w.add_argument("--encoding", default="auto",
                   help="stream mode: metadata text encoding (default auto = utf-8, then cp1250)")

    s = sub.add_parser(
        "show", help="display the logged songs", formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "List logged songs, oldest first, each with its number (#12) and the date and\n"
            "time it was heard. Use the numbers with 'pick', 'search' and 'download'.\n"
            "Without filters, shows the last 50 songs; with --today/--since/--until, all\n"
            "matching songs. --sort, --genre and --unique cover the whole history unless you\n"
            "add a time filter or -n.\n\n"
            "Genres: radios and players don't send a genre, so radiolog looks each song up\n"
            "in free music databases (Deezer, then iTunes) the first time it's needed and\n"
            "remembers the answer. Needs an internet connection the first time."),
        epilog=(
            "time formats for --since / --until:\n"
            "  30m  3h  2d  1w  today  yesterday  20:00  2026-10-18  \"2026-10-18 20:00\"\n\n"
            "examples:\n"
            "  radiolog show\n"
            "  radiolog show --today\n"
            "  radiolog show --since 3h --search queen\n"
            "  radiolog show --since \"2026-10-18 20:00\" --until \"2026-10-18 23:00\"\n"
            "  radiolog show --today --csv today.csv\n"
            "  radiolog show --sort artist --unique   every song once, A-Z by artist, with play counts\n"
            "  radiolog show --sort genre             grouped by genre\n"
            "  radiolog show --genre rock --today     today's rock songs\n"
            "  radiolog show --sort genre --csv songs.csv   export with a genre column"))
    s.add_argument("--today", action="store_true", help="only songs from today")
    s.add_argument("--since", help="e.g. 3h, 2d, yesterday, 20:00, '2026-10-18 20:00'")
    s.add_argument("--until", help="same formats as --since")
    s.add_argument("--search", help="filter by artist or title text")
    s.add_argument("-n", "--limit", type=int, default=None, help="max songs (default: 50 without a time filter)")
    s.add_argument("--csv", metavar="FILE", help="export the selection to a CSV file instead of printing")
    s.add_argument("--no-source", action="store_true", help="hide the station/player column")
    s.add_argument("--sort", choices=["time", "artist", "title", "genre"], default="time",
                   help="order: time (default), artist (A-Z), title (A-Z), or genre (grouped under "
                        "genre headings; genres are looked up online once per song)")
    s.add_argument("--genre", metavar="TEXT", help="only songs whose genre contains TEXT (e.g. rock, pop, rap)")
    s.add_argument("--with-genre", action="store_true", help="show each song's genre in [brackets]")
    s.add_argument("--unique", action="store_true",
                   help="list each song once (latest play) with how often it was played (×3)")
    s.add_argument("--refresh-genres", action="store_true", help="look the genres up again")

    c = sub.add_parser(
        "config", help="show or change where the song history and downloads are stored",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Show or change radiolog's settings: where your song history (database) is\n"
            "stored, where downloaded songs are saved, and their audio format. Without options,\n"
            "shows the current settings, how many songs are logged and whether the history\n"
            "is writable.\n\n"
            "Audio formats:\n"
            + "".join(f"  {k:<5} {v}\n" for k, v in AUDIO_FORMATS.items()) + "\n"
            "Choose locations you can write to without administrator rights. Locations that\n"
            "need them are detected and explained; if you keep one anyway, radiolog must be\n"
            "run elevated (Run as administrator / sudo) every time it uses it."),
        epilog=(
            "examples:\n"
            "  radiolog config                               show current settings\n"
            "  radiolog config --db D:\\Music\\radiolog        move the history into that folder\n"
            "  radiolog config --db \"D:\\My Music\\songs.db\"   use a specific file (quote spaces)\n"
            "  radiolog config --downloads D:\\Music          save downloads there from now on\n"
            "  radiolog config --format mp3                  download songs as .mp3 from now on\n"
            "  radiolog config --reset                       run the first-time setup again\n\n"
            "Settings file: %APPDATA%\\radiolog\\config.json (Linux: ~/.config/radiolog/config.json)."))
    c.add_argument("--db", metavar="LOCATION",
                   help="store the song history here: a folder or a full .db file path. "
                        "Your existing history is moved there (asks first); if a history "
                        "file already exists there, radiolog switches to it instead")
    c.add_argument("--downloads", metavar="FOLDER",
                   help="save future downloads in this folder (already downloaded files stay "
                        "where they are). Default: Music\\radiolog in your user folder")
    c.add_argument("--format", choices=list(AUDIO_FORMATS),
                   help="audio format for downloads: best (default, original stream, usually "
                        ".opus), opus, m4a (no conversion) or mp3 (converted, plays everywhere)")
    c.add_argument("--reset", action="store_true",
                   help="forget all settings (history location, download folder, audio "
                        "format) so the first-time setup runs again on the next start. Your "
                        "history and downloaded songs are NOT deleted")

    def add_filters(sp):
        sp.add_argument("--today", action="store_true", help="only songs from today")
        sp.add_argument("--since", help="e.g. 3h, 2d, yesterday, 20:00, '2026-10-18 20:00'")
        sp.add_argument("--until", help="same formats as --since")
        sp.add_argument("--search", help="filter by artist or title text")
        sp.add_argument("-n", "--limit", type=int, default=None, help="max songs")

    k = sub.add_parser(
        "pick", help="choose songs from a list, then search or download them", formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Interactive menu. Shows the last 20 songs (or those matching the filters),\n"
            "then asks for a song number: Enter picks the latest. Type several numbers or\n"
            "ranges (3 5 8-12) or 'all' to select more than one.\n\n"
            "For one song:   [y] YouTube  [m] YouTube Music  [s] Spotify\n"
            "                [d] download audio  [v] download video  [c] show yt-dlp command\n"
            "For several:    [d] download all (audio)  [v] all (video)  [c] show commands\n"
            "Always:         [b] back to the list  [q] quit\n\n"
            "Audio is saved in your chosen format (see: radiolog config --help)."),
        epilog=(
            "examples:\n"
            "  radiolog pick\n"
            "  radiolog pick --today\n"
            "  radiolog pick --search queen -n 50"))
    add_filters(k)
    k.add_argument("--dir", help="save downloads into this folder this time (setting unchanged)")

    u = sub.add_parser(
        "update", help="update radiolog to the newest release from GitHub",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Check GitHub for a newer radiolog release, show what's new, and install it.\n"
            "Your song history, downloads and settings are kept.\n\n"
            "On Windows the update runs in a new window a moment after radiolog closes,\n"
            "because a running program can't replace itself."),
        epilog=(
            "examples:\n"
            "  radiolog update             check, show what's new, ask, then update\n"
            "  radiolog update --check     only check for a new version\n"
            "  radiolog update -y          update without asking"))
    u.add_argument("--check", action="store_true", help="only check whether a new version exists")
    u.add_argument("-y", "--yes", action="store_true", help="don't ask before updating")
    u.add_argument("--force", action="store_true", help="reinstall even if you have the latest version")

    x = sub.add_parser(
        "clear", help="delete songs from the history (all, chosen numbers, or a time range)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Delete songs from your history.\n"
            "  no arguments      delete EVERYTHING\n"
            "  song numbers      delete those songs (3 5 8-12)\n"
            "  filters           delete the matching songs (--today, --since, --until, --search)\n\n"
            "radiolog shows what will be deleted and asks you to type 'yes'. Before deleting it\n"
            "saves a backup copy of the database next to it (radiolog-backup-<date>.db), so a\n"
            "mistake can be undone. Downloaded songs and settings are never touched."),
        epilog=(
            "examples:\n"
            "  radiolog clear                     delete the whole history (asks first)\n"
            "  radiolog clear 12                  delete song #12\n"
            "  radiolog clear 3 5 8-12            delete several songs\n"
            "  radiolog clear --today             delete today's songs\n"
            "  radiolog clear --until 30d         delete songs older than 30 days\n"
            "  radiolog clear --search \"ad break\" delete entries matching a text (e.g. adverts)\n"
            "  radiolog clear -y --no-backup      no questions, no backup (careful!)"))
    x.add_argument("songs", nargs="*", metavar="SONG",
                   help="song numbers or ranges from 'show' to delete (default: all songs)")
    add_filters(x)
    x.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    x.add_argument("--no-backup", action="store_true", help="don't save a backup copy first")

    f = sub.add_parser(
        "search", help="search a song on YouTube or Spotify (opens the browser)", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Open a search for a logged song (\"Artist - Title\") in your web browser.",
        epilog=(
            "examples:\n"
            "  radiolog search              last song, on YouTube\n"
            "  radiolog search 12           song #12, on YouTube\n"
            "  radiolog search 12 --ytmusic\n"
            "  radiolog search 12 --spotify"))
    f.add_argument("song", nargs="?", default="last", help="song number from 'show' (default: last song)")
    svc = f.add_mutually_exclusive_group()
    svc.add_argument("--yt", "--youtube", dest="service", action="store_const", const="youtube",
                     help="YouTube (default)")
    svc.add_argument("--ytmusic", dest="service", action="store_const", const="ytmusic", help="YouTube Music")
    svc.add_argument("--spotify", dest="service", action="store_const", const="spotify", help="Spotify")
    f.set_defaults(service="youtube")

    d = sub.add_parser(
        "download", help="download songs with yt-dlp in the best quality available", formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Find songs on YouTube (first result for \"Artist - Title\") and download them\n"
            "with yt-dlp in the best quality available.\n\n"
            "Audio is saved in your chosen format (default 'best': the original stream,\n"
            "usually .opus, not converted), with tags and cover art when ffmpeg is installed.\n"
            "--video saves the best video and audio joined into '... (video).mkv'.\n\n"
            "Several songs at once: give several numbers/ranges, or use filters to download\n"
            "everything matching (no 50-song limit). Repeats are downloaded once, songs you\n"
            "already have (in that format) are skipped, and you get a summary at the end.\n\n"
            "Files go to the download folder from your settings (see: radiolog config)."),
        epilog=(
            "examples:\n"
            "  radiolog download                     last song\n"
            "  radiolog download 12                  song #12\n"
            "  radiolog download 3 5 8-12            several songs and a range\n"
            "  radiolog download --today             everything heard today (asks first)\n"
            "  radiolog download --since 3h -y       last 3 hours, without asking\n"
            "  radiolog download 12 --video\n"
            "  radiolog download 12 --format mp3     as .mp3 this time\n"
            "  radiolog download 12 --print-cmd      only show the yt-dlp command"))
    d.add_argument("songs", nargs="*", metavar="SONG",
                   help="song numbers or ranges from 'show', e.g. 12  3 5 7  10-15 (default: last song)")
    add_filters(d)
    d.add_argument("--video", action="store_true", help="download the video instead of audio only")
    d.add_argument("--dir", help="save into this folder instead of the default")
    d.add_argument("--print-cmd", action="store_true", help="only print the yt-dlp commands, don't run them")
    d.add_argument("-y", "--yes", action="store_true", help="don't ask before a batch download")
    d.add_argument("--force", action="store_true", help="download again even if the file already exists")
    d.add_argument("--format", choices=list(AUDIO_FORMATS),
                   help="audio format for this download (default: your setting, see radiolog config)")

    p.add_argument("--version", action="version",
                   version=f"radiolog {installed_version() or 'dev (not installed)'}")
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
        if args.cmd == "update":
            cmd_update(args)
            return
        DB_PATH = resolve_db_path()
        if args.cmd == "clear":
            cmd_clear(args)
            return
        if args.cmd == "pick":
            cmd_pick(args)
            return
        if args.cmd == "search":
            cmd_search(args)
            return
        if args.cmd == "download":
            cmd_download(args)
            return
        if args.cmd == "watch":
            rec = Recorder(db_connect())
            print(f"Saving to {DB_PATH}")
            if args.url:
                watch_stream(args.url, rec, args.encoding)
            else:
                watch_system(rec, args.interval, args.player)
        else:
            cmd_show(args)
    except KeyboardInterrupt:
        print("\nStopped.")
    except BrokenPipeError:          # output piped into e.g. 'head' that closed early
        sys.stderr.close()


if __name__ == "__main__":
    main()
