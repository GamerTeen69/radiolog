"""
radiolog_spotify - Spotify features for radiolog.

  * open_in_app():   open a Spotify search in the installed Spotify app (spotify: link)
  * SpotifyClient:   log in (Authorization Code with PKCE, no client secret), find songs,
                     create playlists and add songs, using the user's OWN Spotify app
                     (Client ID from https://developer.spotify.com/dashboard).

Spotify rules this code follows (Web API, 2026):
  - redirect URI must be a loopback IP literal, not 'localhost': http://127.0.0.1:8888/callback
  - development-mode apps: owner needs Premium; up to 5 allowlisted users
  - create playlist:  POST /v1/me/playlists
  - add songs:        POST /v1/playlists/{id}/items   (max 100 URIs per request)
  - search:           GET  /v1/search  (limit max 10)
"""
import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

IS_WIN = sys.platform == "win32"

# overridable for testing
ACCOUNTS = os.environ.get("RADIOLOG_SPOTIFY_ACCOUNTS", "https://accounts.spotify.com")
API = os.environ.get("RADIOLOG_SPOTIFY_API", "https://api.spotify.com/v1")
REDIRECT_PORT = 8888
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}/callback"
SCOPES = "playlist-modify-private playlist-modify-public playlist-read-private"
SETUP_HELP = f"""\
To create Spotify playlists, radiolog needs your own (free) Spotify developer app.
It takes about 5 minutes, once. You need Spotify Premium.

  1. Open https://developer.spotify.com/dashboard and log in with your Spotify account.
  2. Click "Create app" and fill in:
       App name:        radiolog   (anything)
       App description: anything
       Redirect URI:    {REDIRECT_URI}      <- exactly this, then "Add"
       APIs used:       tick "Web API"
     Accept the terms and click Save.
  3. Open the app's Settings and copy the Client ID (the secret is NOT needed).
  4. Run:  radiolog config --spotify-client-id YOUR_CLIENT_ID

The first time you create a playlist, your browser asks you to allow radiolog.
Your password never goes through radiolog."""


class SpotifyError(Exception):
    pass


# ----------------------------------------------------------------------------- open in app

def app_installed():
    """Is something registered to open spotify: links (the Spotify desktop app)?"""
    if IS_WIN:
        try:
            import winreg
            # normal installer: a 'spotify' URL protocol
            for root, path in ((winreg.HKEY_CURRENT_USER, r"Software\Classes\spotify"),
                               (winreg.HKEY_CLASSES_ROOT, "spotify")):
                try:
                    with winreg.OpenKey(root, path) as key:
                        winreg.QueryValueEx(key, "URL Protocol")
                        return True
                except OSError:
                    continue
            # Microsoft Store version: packaged apps register protocols here instead
            try:
                winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                               r"Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion"
                               r"\AppModel\PackageRepository\Extensions\windows.protocol\spotify").Close()
                return True
            except OSError:
                pass
        except ImportError:
            pass
        return False
    if sys.platform == "darwin":
        return any(Path(p).exists() for p in ("/Applications/Spotify.app",
                                              str(Path.home() / "Applications/Spotify.app")))
    if shutil.which("xdg-mime"):
        try:
            r = subprocess.run(["xdg-mime", "query", "default", "x-scheme-handler/spotify"],
                               capture_output=True, text=True, timeout=5)
            return bool(r.stdout.strip())
        except Exception:
            return False
    return False


def open_uri(uri):
    """Open a spotify: link with the system's handler. True if it was handed over."""
    try:
        if IS_WIN:
            os.startfile(uri)                         # noqa: (Windows only)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", uri])
        else:
            subprocess.Popen(["xdg-open", uri], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def search_uri(query):
    return "spotify:search:" + urllib.parse.quote(query, safe="")


# ----------------------------------------------------------------------------- matching

def _norm(s):
    """Lowercase, no accents, no punctuation, no '(Remastered)' style additions."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = s.replace("ł", "l").replace("ø", "o").replace("ß", "ss")
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
    s = re.sub(r"\s-\s.*?(remaster|version|edit|mix|live|mono|stereo).*$", " ", s)
    s = re.sub(r"\b(feat|ft)\b.*$", " ", s)
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _similar(a, b):
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.85
    from difflib import SequenceMatcher
    return SequenceMatcher(None, a, b).ratio()


def match_score(artist, title, track):
    """How well a Spotify track matches 'artist - title' (0..1)."""
    t = _similar(title, track.get("name", ""))
    names = [a.get("name", "") for a in track.get("artists", []) or []]
    a = max((_similar(artist, n) for n in names), default=0.0) if artist else 0.7
    if artist and a < 0.5:                         # maybe several artists in one field
        a = max(a, _similar(artist, ", ".join(names)), _similar(artist, " & ".join(names)))
    return 0.55 * t + 0.45 * a


# ----------------------------------------------------------------------------- client

class SpotifyClient:
    def __init__(self, client_id, token_file, user_agent="radiolog", say=print):
        if not client_id:
            raise SpotifyError("no-client-id")
        self.client_id = client_id.strip()
        self.token_file = Path(token_file)
        self.user_agent = user_agent
        self.say = say
        self.token = self._load()

    # ------------------------------------------------------------- tokens
    def _load(self):
        try:
            data = json.loads(self.token_file.read_text(encoding="utf-8"))
            return data if data.get("client_id") == self.client_id else None
        except (OSError, ValueError):
            return None

    def _save(self):
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(json.dumps(self.token, indent=2), encoding="utf-8")
        try:
            os.chmod(self.token_file, 0o600)
        except OSError:
            pass

    def logout(self):
        self.token = None
        try:
            self.token_file.unlink()
        except FileNotFoundError:
            pass

    def _token_request(self, fields):
        data = urllib.parse.urlencode(dict(fields, client_id=self.client_id)).encode()
        req = urllib.request.Request(f"{ACCOUNTS}/api/token", data=data, headers={
            "Content-Type": "application/x-www-form-urlencoded", "User-Agent": self.user_agent})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                tok = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            raise SpotifyError(f"Spotify login failed ({e.code}): {body[:200]}")
        old_refresh = (self.token or {}).get("refresh_token")
        self.token = {
            "client_id": self.client_id,
            "access_token": tok["access_token"],
            "refresh_token": tok.get("refresh_token") or old_refresh,   # may rotate
            # renew a little before it runs out (Spotify tokens last an hour)
            "expires_at": time.time() + int(tok.get("expires_in", 3600)) * 0.9,
            "scope": tok.get("scope", SCOPES),
        }
        self._save()

    def login(self, open_browser=True, timeout=300):
        """Authorization Code with PKCE: browser consent, code comes back to 127.0.0.1:8888."""
        verifier = secrets.token_urlsafe(64)[:96]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        state = secrets.token_urlsafe(16)
        url = f"{ACCOUNTS}/authorize?" + urllib.parse.urlencode({
            "client_id": self.client_id, "response_type": "code", "redirect_uri": REDIRECT_URI,
            "code_challenge_method": "S256", "code_challenge": challenge, "state": state,
            "scope": SCOPES})
        result = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(h):
                q = urllib.parse.parse_qs(urllib.parse.urlsplit(h.path).query)
                if not h.path.startswith("/callback"):
                    h.send_response(404); h.end_headers(); return
                if q.get("state", [""])[0] != state:
                    result["error"] = "state mismatch (try again)"
                elif "error" in q:
                    result["error"] = q["error"][0]
                else:
                    result["code"] = q.get("code", [""])[0]
                ok = "code" in result
                page = ("<h2>radiolog is connected to Spotify ✔</h2><p>You can close this tab.</p>" if ok else
                        f"<h2>Spotify login didn't work</h2><p>{result.get('error')}</p>")
                h.send_response(200)
                h.send_header("Content-Type", "text/html; charset=utf-8")
                h.end_headers()
                h.wfile.write(f"<html><body style='font-family:sans-serif'>{page}</body></html>".encode())

            def log_message(h, *a):
                pass

        try:
            server = HTTPServer(("127.0.0.1", REDIRECT_PORT), Handler)
        except OSError as e:
            raise SpotifyError(f"Port {REDIRECT_PORT} is busy ({e}). Close the program using it and try again.")
        server.timeout = 1
        self.say("Log in to Spotify in your browser and click 'Agree'…")
        if not open_browser or not webbrowser.open(url):
            self.say(f"Open this link in your browser:\n  {url}")
        deadline = time.time() + timeout
        try:
            while not result and time.time() < deadline:
                server.handle_request()
        finally:
            server.server_close()
        if "code" not in result:
            err = result.get("error", "timed out")
            if err == "access_denied":
                raise SpotifyError("You didn't allow access, so nothing was created.")
            if "INVALID_CLIENT" in str(err).upper() or "redirect" in str(err).lower():
                raise SpotifyError(f"Spotify refused the login ({err}). Check the Client ID and that the "
                                   f"Redirect URI in your Spotify app is exactly {REDIRECT_URI}")
            raise SpotifyError(f"Spotify login didn't finish ({err}).")
        self._token_request({"grant_type": "authorization_code", "code": result["code"],
                             "redirect_uri": REDIRECT_URI, "code_verifier": verifier})
        self.say("✔ Connected to Spotify.")

    def ensure_login(self):
        if self.token and self.token.get("access_token"):
            if time.time() < self.token.get("expires_at", 0):
                return
            if self.token.get("refresh_token"):
                try:
                    self._token_request({"grant_type": "refresh_token",
                                         "refresh_token": self.token["refresh_token"]})
                    return
                except SpotifyError:
                    pass                               # refresh token revoked: log in again
        self.login()

    # ------------------------------------------------------------- API
    def api(self, method, path, params=None, body=None, retry=True):
        self.ensure_login()
        url = path if path.startswith("http") else f"{API}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token['access_token']}", "User-Agent": self.user_agent,
            **({"Content-Type": "application/json"} if data is not None else {})})
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    raw = r.read()
                    return json.loads(raw.decode("utf-8")) if raw else {}
            except urllib.error.HTTPError as e:
                text = e.read().decode("utf-8", "replace")
                if e.code == 429:
                    wait = int(e.headers.get("Retry-After", "2") or 2)
                    if "QUOTA" in text.upper() or wait > 120:
                        raise SpotifyError("Spotify's limit for your app was reached. Try again later.")
                    time.sleep(min(wait, 60) + 0.5)
                    continue
                if e.code == 401 and retry:
                    self.token["expires_at"] = 0       # force a refresh, then try once more
                    return self.api(method, path, params, body, retry=False)
                if e.code == 403:
                    raise SpotifyError(
                        "Spotify said 'forbidden' (403). Usually: the app owner's Premium ran out, "
                        "or this Spotify account isn't on the app's User Management list. "
                        f"Details: {text[:200]}")
                if e.code >= 500 and attempt < 4:
                    time.sleep(1 + attempt)
                    continue
                raise SpotifyError(f"Spotify error {e.code}: {text[:200]}")
        raise SpotifyError("Spotify kept saying 'too many requests'. Try again later.")

    def me(self):
        return self.api("GET", "/me")

    def find_track(self, artist, title):
        """Best matching track: dict(uri, name, artists, score) or None."""
        clean_title = re.sub(r"\s*[\(\[].*?[\)\]]\s*", " ", title).strip() or title
        queries = []
        if artist:
            queries.append(f'track:"{clean_title}" artist:"{artist}"')
        queries.append(f"{artist} {clean_title}".strip())
        best = None
        for q in queries:
            res = self.api("GET", "/search", {"q": q, "type": "track", "limit": 10})
            for tr in ((res.get("tracks") or {}).get("items") or []):
                if not tr or not tr.get("uri"):
                    continue
                s = match_score(artist, title, tr)
                if not best or s > best["score"]:
                    best = dict(uri=tr["uri"], name=tr.get("name", ""), score=s,
                                artists=", ".join(a.get("name", "") for a in tr.get("artists", []) or []),
                                url=(tr.get("external_urls") or {}).get("spotify", ""))
            if best and best["score"] >= 0.8:
                break
        return best if best and best["score"] >= 0.6 else None

    def my_playlists(self):
        out, url, params = [], "/me/playlists", {"limit": 50}
        while url:
            res = self.api("GET", url, params)
            out += [p for p in res.get("items") or [] if p]
            url, params = res.get("next"), None
        return out

    def create_playlist(self, name, description="", public=False):
        return self.api("POST", "/me/playlists",
                        body={"name": name, "description": description, "public": bool(public)})

    def playlist_uris(self, playlist_id):
        uris, url, params = set(), f"/playlists/{playlist_id}/items", {"limit": 100,
                                                                        "fields": "items(item(uri),track(uri)),next"}
        while url:
            res = self.api("GET", url, params)
            for it in res.get("items") or []:
                obj = (it or {}).get("item") or (it or {}).get("track") or {}
                if obj.get("uri"):
                    uris.add(obj["uri"])
            url, params = res.get("next"), None
        return uris

    def add_items(self, playlist_id, uris):
        for i in range(0, len(uris), 100):
            self.api("POST", f"/playlists/{playlist_id}/items", body={"uris": uris[i:i + 100]})
