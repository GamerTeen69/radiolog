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

Data is stored in ~/.radiolog.db (override with the RADIOLOG_DB environment variable).
"""
import argparse
import csv
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

DB_PATH = Path(os.environ.get("RADIOLOG_DB", str(Path.home() / ".radiolog.db")))
USER_AGENT = "radiolog/1.0"
WIN_PKGS = "winrt-runtime winrt-Windows.Media.Control winrt-Windows.Foundation winrt-Windows.Foundation.Collections winrt-Windows.Storage.Streams"


# --------------------------------------------------------------------------- storage

def db_connect():
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

    args = p.parse_args()

    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except AttributeError:
            pass

    try:
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
