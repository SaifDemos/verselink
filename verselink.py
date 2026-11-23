#!/usr/bin/env python3
"""
VerseLink - fullscreen line-synced lyrics view (Spicy Lyrics style) for desktop players.

Linux: reads the current track from `playerctl` (MPRIS), loads the .lrc file
sitting next to the audio file (falls back to lrclib.net when online), and serves
a page on the LAN so you can display it fullscreen on a laptop and control it
from a tablet/phone.

Windows: uses the Windows Global System Media Transport Controls (GSMTC) through
PowerShell, so any app that integrates with Windows media controls (Spotify,
browsers, VLC, Media Player, ...) works. No extra Python packages are needed.

Usage:
    python3 verselink.py --player strawberry
    # then open http://<this-machine-ip>:8765 on the tablet too (same Wi-Fi)

    ./start.sh / ./stop.sh             (Linux, background service)
    .\\start.ps1 / .\\stop.ps1           (Windows, background service)

Options:
    --player NAME    initial player/source id (default: automatic)
    --host 0.0.0.0   bind address (default: all interfaces; use 127.0.0.1 to stay local)
    --port 8765      HTTP port

On screen: lyrics are highlighted line by line (no word-by-word timing). Move the
mouse or tap to reveal the transport bar and seek bar; both fade away when idle.
On a tablet, swipe right on the lyrics for the next track and swipe left for the
previous one. Hover the top-right corner (or tap it) for the settings panel:
audio source (apps currently open), position of lyrics/cover, full-cover mode,
animation level, background (liquid/blur/colors/dark), text and cover size, and
display toggles. Settings are stored in the browser, so each device can have its
own layout; the selected audio source is shared by the server.

Note: binding on all interfaces lets anyone on your network control the player
(play/pause/next/seek). Use --host 127.0.0.1 if you don't want that.

Needs: Linux -> playerctl; Windows -> PowerShell 5.1 (built in); python 3.8+.
No Python dependencies on either platform.
"""
import argparse
import base64
import hashlib
import json
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SEP = "\x1f"
FIELDS = ["{{status}}", "{{position}}", "{{mpris:length}}", "{{xesam:url}}",
          "{{artist}}", "{{title}}", "{{album}}", "{{mpris:artUrl}}"]
FMT = SEP.join(FIELDS)
IS_WINDOWS = sys.platform.startswith("win")
PLAYER = None
STATE_LOCK = threading.Lock()
WIN_HELPER = Path(__file__).with_name("win-gsmtc.ps1")

_cache_root = Path.home() / ".cache"
CACHE_DIR = _cache_root / "verselink"
_legacy_cache = _cache_root / "synclyrics"
if not CACHE_DIR.exists() and _legacy_cache.exists():
    CACHE_DIR = _legacy_cache  # keep previously downloaded lyrics available offline
lyrics_cache = {}


# ----------------------------------------------------------------- player ---
_APP_NAMES = {
    "spotify": "Spotify", "vlc": "VLC", "firefox": "Firefox",
    "chromium": "Chromium", "chrome": "Chrome", "brave": "Brave",
    "msedge": "Edge", "edge": "Edge", "opera": "Opera", "mpv": "mpv",
    "strawberry": "Strawberry", "rhythmbox": "Rhythmbox",
    "clementine": "Clementine", "audacious": "Audacious", "elisa": "Elisa",
    "amberol": "Amberol", "tidal": "TIDAL", "deezer": "Deezer",
    "applemusic": "Apple Music", "itunes": "iTunes", "amazonmusic": "Amazon Music",
    "zunemusic": "Media Player", "foobar2000": "foobar2000",
}


def pretty_name(pid):
    """Turn a playerctl/GSMTC app id into something readable in the UI."""
    low = pid.lower()
    for hint in sorted(_APP_NAMES, key=len, reverse=True):
        if hint in low:
            return _APP_NAMES[hint]
    base = re.split(r"[.!]", pid)[0].replace("_", " ").replace("-", " ")
    return base.title() or pid


def playerctl(*args, timeout=2, use_player=True):
    cmd = ["playerctl"]
    if use_player and PLAYER:
        cmd += ["-p", PLAYER]
    cmd += list(args)
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def win_bridge(op, timeout=15, **params):
    """Run one GSMTC query through PowerShell and return the parsed JSON."""
    cmd = ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
           "-ExecutionPolicy", "Bypass", "-File", str(WIN_HELPER), "-Op", op]
    for key, value in params.items():
        cmd += ["-" + key, str(value)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    stdout = r.stdout.decode("utf-8", "replace")
    for line in reversed(stdout.splitlines()):
        line = line.strip().lstrip("\ufeff")
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return None


def list_players():
    """Apps/sources that are currently open and expose media controls."""
    if IS_WINDOWS:
        data = win_bridge("list") or {}
        ids = data.get("players") or []
    else:
        r = playerctl("-l", use_player=False)
        ids = (r.stdout.splitlines() if r and r.returncode == 0 else [])
    out, seen = [], set()
    for pid in ids:
        pid = pid.strip()
        if pid and pid not in seen:
            seen.add(pid)
            out.append({"id": pid, "name": pretty_name(pid)})
    return out


def active_player(players):
    """Selected player, falling back to automatic when it is gone."""
    global PLAYER
    with STATE_LOCK:
        if PLAYER and PLAYER not in {p["id"] for p in players}:
            PLAYER = None
        return PLAYER


def set_player(pid):
    global PLAYER
    with STATE_LOCK:
        PLAYER = pid or None
    _win_invalidate()


def _linux_meta():
    r = playerctl("metadata", "--format", FMT)
    if not r or r.returncode != 0 or not r.stdout.strip():
        return None
    parts = r.stdout.rstrip("\n").split(SEP)
    if len(parts) < len(FIELDS):
        return None
    status, pos, length, url, artist, title, album, art = parts[:8]

    def secs(x):
        try:
            return float(x) / 1e6
        except ValueError:
            return 0.0

    return {"status": status, "pos": secs(pos), "dur": secs(length), "url": url,
            "artist": artist, "title": title, "album": album, "art": art}


_WIN_STATE = {"ts": 0.0, "data": None}
_WIN_LOCK = threading.Lock()
_WIN_STATE_TTL = 1.2  # PowerShell/GSMTC calls are expensive; the UI extrapolates


def _win_invalidate():
    with _WIN_LOCK:
        _WIN_STATE["ts"] = 0.0
        _WIN_STATE["data"] = None


def _win_meta():
    now = time.monotonic()
    with _WIN_LOCK:
        ts, cached = _WIN_STATE["ts"], _WIN_STATE["data"]
        if cached and now - ts < _WIN_STATE_TTL:
            d = dict(cached)
            if d.get("status") == "Playing":
                pos = d.get("pos", 0.0) + (now - ts)
                dur = d.get("dur", 0.0)
                d["pos"] = min(pos, dur) if dur > 0 else pos
            return d
    data = win_bridge("state", Player=PLAYER or "")
    if not data or data.get("status") == "none":
        data = None
    with _WIN_LOCK:
        _WIN_STATE["ts"] = now if data else 0.0
        _WIN_STATE["data"] = data
    return dict(data) if data else None


def _win_platform_meta():
    d = _win_meta()
    if not d:
        return None
    amid = str(d.get("id") or "")
    title = str(d.get("title") or "")
    artist = str(d.get("artist") or "")
    try:
        pos = float(d.get("pos") or 0)
        dur = float(d.get("dur") or 0)
    except (TypeError, ValueError):
        pos = dur = 0.0
    tag = hashlib.md5(f"{title}|{artist}".encode()).hexdigest()[:8]
    return {"status": str(d.get("status") or "Paused"),
            "pos": max(0.0, pos), "dur": max(0.0, dur),
            "url": "win:" + amid, "artist": artist, "title": title,
            "album": str(d.get("album") or ""),
            "art": f"win:{amid}:{tag}" if amid else ""}


def get_meta():
    raw = _win_platform_meta() if IS_WINDOWS else _linux_meta()
    if raw is None and PLAYER:
        # The selected app closed (or lost its media session): go automatic.
        if PLAYER not in {p["id"] for p in list_players()}:
            set_player(None)
            raw = _win_platform_meta() if IS_WINDOWS else _linux_meta()
    if not raw:
        return None
    tid = hashlib.md5(
        f"{raw['url']}|{raw['artist']}|{raw['title']}".encode()).hexdigest()[:12]
    raw["id"] = tid
    return raw


def send_command(c):
    if c not in ("play-pause", "next", "previous"):
        return False
    if IS_WINDOWS:
        _win_invalidate()
        return bool((win_bridge("cmd", Player=PLAYER or "", Command=c) or {}).get("ok"))
    r = playerctl(c)
    return bool(r and r.returncode == 0)


def seek_to(t):
    if IS_WINDOWS:
        _win_invalidate()
        win_bridge("seek", Player=PLAYER or "", Seconds=f"{t:.3f}")
    else:
        playerctl("position", f"{t:.3f}")


# ----------------------------------------------------------------- lyrics ---
TS = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
WORD_TAG = re.compile(r"<\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?>")


def parse_lrc(text):
    """Parse a .lrc file into [[seconds, text], ...] (line timing only).

    Enhanced-LRC word timestamps are stripped from the text: this view syncs
    line by line, so per-word timings aren't needed.
    """
    lines, offset = [], 0.0
    for raw in text.splitlines():
        raw = raw.strip()
        m = re.match(r"\[offset:\s*([+-]?\d+)\]", raw, re.I)
        if m:
            offset = int(m.group(1)) / 1000.0
            continue
        stamps = list(TS.finditer(raw))
        if not stamps:
            continue
        body = raw[stamps[-1].end():]
        lyric = WORD_TAG.sub("", body).strip()
        for s in stamps:
            frac = s.group(3) or "0"
            t = int(s.group(1)) * 60 + int(s.group(2)) + int(frac) / (10 ** len(frac))
            lines.append([round(max(0.0, t - offset), 3), lyric])
    lines.sort(key=lambda x: x[0])
    return lines


def find_lrc(url):
    if not url.startswith("file://"):
        return None
    p = Path(urllib.parse.unquote(urllib.parse.urlparse(url).path))
    for ext in (".lrc", ".LRC"):
        cand = p.with_suffix(ext)
        if cand.is_file():
            return cand
    return None


_UA = {"User-Agent": "verselink/1.0 (local lyrics display)"}


def _lrclib(endpoint, params):
    url = "https://lrclib.net/api/" + endpoint + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=_UA)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.load(resp)
    except Exception:
        return None


def fetch_online(meta):
    """Look lyrics up on LRCLIB; exact match first, then search by title/artist.

    Results are cached on disk by track id, so a song whose lyrics were fetched
    once still shows them when the machine is offline later.
    """
    cache = CACHE_DIR / f"{meta['id']}.lrc"
    if cache.is_file():
        return cache.read_text(encoding="utf-8", errors="replace")
    artist = meta["artist"].split("/")[0].strip()
    track = meta["title"].strip()
    text = ""
    data = _lrclib("get", {"artist_name": artist, "track_name": track,
                           "album_name": meta["album"], "duration": int(meta["dur"])})
    if isinstance(data, dict):
        text = data.get("syncedLyrics") or ""
    if not text:
        results = _lrclib("search", {"artist_name": artist, "track_name": track})
        if isinstance(results, list):
            best, best_score = None, None
            for item in results:
                if not isinstance(item, dict) or not item.get("syncedLyrics"):
                    continue
                try:
                    score = abs(float(item.get("duration") or 0) - float(meta["dur"]))
                except (TypeError, ValueError):
                    score = 1e9
                if best_score is None or score < best_score:
                    best, best_score = item, score
            if best:
                text = best["syncedLyrics"]
    if text:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_text(text, encoding="utf-8")
    return text


def load_lyrics(meta):
    tid = meta["id"]
    if tid in lyrics_cache:
        return lyrics_cache[tid]
    text, source = "", "none"
    p = find_lrc(meta["url"])
    if p:
        try:
            text, source = p.read_text(encoding="utf-8-sig", errors="replace"), "local"
        except OSError:
            pass
    if not text:
        text = fetch_online(meta)
        source = "online" if text else "none"
    lines = parse_lrc(text) if text else []
    if not lines:
        source = "none"
    result = {"id": tid, "lines": lines, "source": source}
    lyrics_cache[tid] = result
    if len(lyrics_cache) > 200:
        lyrics_cache.pop(next(iter(lyrics_cache)))
    return result


# -------------------------------------------------------------------- art ---
ART_CACHE = CACHE_DIR / "art"


def sniff_image(data):
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] == b"GIF8":
        return "image/gif"
    if data[:2] == b"BM":
        return "image/bmp"
    return None


def _store_art(key, data):
    try:
        ART_CACHE.mkdir(parents=True, exist_ok=True)
        (ART_CACHE / key).write_bytes(data)
        if len(list(ART_CACHE.iterdir())) > 120:
            for old in sorted(ART_CACHE.iterdir(), key=lambda p: p.stat().st_mtime)[:40]:
                old.unlink(missing_ok=True)
    except OSError:
        pass


def fetch_art(url):
    """Download remote artwork (Spotify, etc.) once and keep it on disk."""
    key = hashlib.md5(url.encode()).hexdigest()
    cache = ART_CACHE / key
    try:
        return cache.read_bytes()
    except OSError:
        pass
    req = urllib.request.Request(url, headers=_UA)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = resp.read(12 * 1024 * 1024)
    except Exception:
        return None
    if data:
        _store_art(key, data)
    return data


def _win_art(art):
    """Fetch the current GSMTC thumbnail for a track (cached like remote art)."""
    key = hashlib.md5(art.encode()).hexdigest()
    cache = ART_CACHE / key
    try:
        return cache.read_bytes()
    except OSError:
        pass
    session = art[4:].rsplit(":", 1)[0]
    data = win_bridge("art", Player=session) or {}
    try:
        raw = base64.b64decode(data.get("b64") or "")
    except (ValueError, TypeError):
        return None
    if not raw:
        return None
    _store_art(key, raw)
    return raw


def read_art(meta):
    art = meta["art"] if meta else ""
    if not art:
        return None, None
    if art.startswith("win:"):
        data = _win_art(art)
    elif art.startswith("file://"):
        p = Path(urllib.parse.unquote(urllib.parse.urlparse(art).path))
        try:
            data = p.read_bytes()
        except OSError:
            return None, None
    elif art.startswith(("http://", "https://")):
        data = fetch_art(art)
    else:
        return None, None
    if not data:
        return None, None
    ctype = sniff_image(data)
    return (data, ctype) if ctype else (None, None)


# ------------------------------------------------------------------- http ---
class Handler(BaseHTTPRequestHandler):
    server_version = "VerseLink"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def reply(self, code, body, ctype="application/json", cache="no-store"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            self.reply(200, PAGE, "text/html; charset=utf-8")
        elif path == "/state":
            m = get_meta()
            if not m:
                return self.reply(200, {"status": "none"})
            art = m["art"]
            if art:
                art = "/art?v=" + hashlib.md5(art.encode()).hexdigest()[:10]
            self.reply(200, {"status": m["status"], "pos": m["pos"], "dur": m["dur"],
                             "id": m["id"], "title": m["title"], "artist": m["artist"],
                             "album": m["album"], "art": art})
        elif path == "/lyrics":
            m = get_meta()
            self.reply(200, load_lyrics(m) if m else {"id": None, "lines": [], "source": "none"})
        elif path == "/players":
            players = list_players()
            self.reply(200, {"players": players, "active": active_player(players)})
        elif path == "/art":
            data, ctype = read_art(get_meta())
            if data:
                self.reply(200, data, ctype, cache="max-age=3600")
            else:
                self.reply(404, "")
        elif path == "/ping":
            self.reply(200, {"ok": True})
        else:
            self.reply(404, "")

    def do_POST(self):
        # custom header forces a CORS preflight, so other websites can't call this
        if self.headers.get("X-Requested-With") != "verselink":
            return self.reply(403, "")
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/seek":
            try:
                t = max(0.0, float(q.get("t", ["0"])[0]))
            except ValueError:
                return self.reply(400, "")
            seek_to(t)
            self.reply(200, {"ok": True})
        elif u.path == "/cmd":
            c = q.get("c", [""])[0]
            if c in ("play-pause", "next", "previous"):
                send_command(c)
                self.reply(200, {"ok": True})
            else:
                self.reply(400, "")
        elif u.path == "/player":
            pid = q.get("p", [""])[0].strip()
            players = list_players()
            if pid and pid not in {p["id"] for p in players}:
                return self.reply(409, {"ok": False, "players": players,
                                        "active": active_player(players)})
            set_player(pid)
            self.reply(200, {"ok": True, "players": players,
                             "active": active_player(players)})
        else:
            self.reply(404, "")


# ------------------------------------------------------------------- page ---
PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>VerseLink</title>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#000000">
<style>
*{box-sizing:border-box}
:root{
  --c1:#3a5f8a; --c2:#7a3a8a;
  --c1a:rgba(58,95,138,.75); --c2a:rgba(122,58,138,.75);
  --glow:rgba(140,170,220,.35);
  --ls:1; --cs:1; --lt:.55s; --st:.72s;
  --liquid-a:16s; --liquid-b:21s; --liquid-c:28s;
}
html,body{margin:0;height:100%;background:#000;color:#fff;overflow:hidden;
  font-family:"Inter","Noto Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
  -webkit-tap-highlight-color:transparent;touch-action:manipulation;
  overscroll-behavior:none;-webkit-touch-callout:none}
body{-webkit-user-select:none;user-select:none}
body.idle{cursor:none}

/* background ------------------------------------------------------------- */
#bg{position:fixed;inset:-12%;background:#0b0b10 center/cover no-repeat;
  background-image:var(--art,none);
  filter:blur(62px) brightness(.4) saturate(1.35);transform:scale(1.03);
  z-index:0;pointer-events:none;transition:filter .5s,transform .5s}
/* Independently flowing patches of the blurred artwork, not audio bars. */
#liquid{position:fixed;inset:0;z-index:1;overflow:hidden;pointer-events:none;
  contain:layout paint;isolation:isolate;opacity:0;visibility:hidden;
  transition:opacity .8s,visibility .8s}
.liquid-layer{position:absolute;left:50%;top:50%;width:110vmax;height:95vmax;
  background-image:var(--liquid-art,var(--art,none)),
    radial-gradient(ellipse at 30% 40%,var(--c1a),transparent 68%),
    radial-gradient(ellipse at 70% 60%,var(--c2a),transparent 68%);
  background-position:center;background-size:cover;background-repeat:no-repeat;
  filter:blur(clamp(24px,4vmin,56px)) saturate(1.5) brightness(.8);
  -webkit-mask-image:radial-gradient(ellipse at center,#000 18%,rgba(0,0,0,.8) 38%,transparent 68%);
  mask-image:radial-gradient(ellipse at center,#000 18%,rgba(0,0,0,.8) 38%,transparent 68%)}
.liquid-a{opacity:.75;transform:translate(-62%,-58%) rotate(-12deg) scale(1.08)}
.liquid-b{opacity:.6;mix-blend-mode:screen;
  transform:translate(-32%,-32%) rotate(115deg) scale(.88)}
.liquid-c{opacity:.5;mix-blend-mode:screen;
  transform:translate(-72%,-36%) rotate(220deg) scale(.78)}
body[data-bg="liquid"] #liquid{opacity:.9;visibility:visible}
body[data-bg="liquid"] .liquid-a{
  animation:liquidA var(--liquid-a) ease-in-out -5s infinite}
body[data-bg="liquid"] .liquid-b{
  animation:liquidB var(--liquid-b) ease-in-out -11s infinite}
body[data-bg="liquid"] .liquid-c{
  animation:liquidC var(--liquid-c) ease-in-out -17s infinite}
@keyframes liquidA{
  0%,100%{transform:translate(-62%,-58%) rotate(-12deg) scale(1.08)}
  25%{transform:translate(-44%,-72%) rotate(18deg) scale(1.18)}
  50%{transform:translate(-38%,-42%) rotate(42deg) scale(1)}
  75%{transform:translate(-68%,-34%) rotate(8deg) scale(1.14)}
}
@keyframes liquidB{
  0%,100%{transform:translate(-32%,-32%) rotate(115deg) scale(.88)}
  30%{transform:translate(-58%,-38%) rotate(145deg) scale(1.08)}
  65%{transform:translate(-62%,-65%) rotate(175deg) scale(.94)}
}
@keyframes liquidC{
  0%,100%{transform:translate(-72%,-36%) rotate(220deg) scale(.78)}
  35%{transform:translate(-38%,-48%) rotate(190deg) scale(.92)}
  70%{transform:translate(-48%,-76%) rotate(250deg) scale(.84)}
}
body[data-anim="subtle"]{--liquid-a:30s;--liquid-b:38s;--liquid-c:46s}
body[data-anim="cinematic"]{--liquid-a:10s;--liquid-b:14s;--liquid-c:18s}
body[data-bg="liquid"][data-bg-motion="1"] .liquid-layer{will-change:transform}
body[data-bg="liquid"][data-bg-motion="0"] .liquid-layer{animation-play-state:paused}
@media (prefers-reduced-motion: reduce){
  body[data-bg="liquid"] .liquid-layer{animation-play-state:paused}
}
#shade{position:fixed;inset:0;z-index:2;pointer-events:none;
  background:radial-gradient(ellipse at 26% 34%,rgba(255,255,255,.07),transparent 62%),
             linear-gradient(rgba(0,0,0,.16),rgba(0,0,0,.42))}
body[data-bg="colors"] #bg{
  background-image:
    radial-gradient(70% 85% at 20% 22%, var(--c1a), transparent 70%),
    radial-gradient(75% 90% at 80% 78%, var(--c2a), transparent 72%),
    linear-gradient(#07070b,#07070b);
  filter:none;transform:none}
body[data-bg="dark"] #bg{background-image:none;filter:none;transform:none;background:#07070b}

/* stage ------------------------------------------------------------------ */
#stage{position:relative;z-index:5;display:flex;align-items:center;
  gap:clamp(18px,3vw,56px);height:100vh;height:100dvh;padding:0 clamp(18px,4vw,64px)}
#lyricsWrap{flex:1 1 auto;min-width:0;height:100%;display:flex}
#lyrics{position:relative;flex:1;min-width:0;overflow:hidden;
  -webkit-mask-image:linear-gradient(transparent,#000 15%,#000 80%,transparent);
  mask-image:linear-gradient(transparent,#000 15%,#000 80%,transparent)}
#track{position:absolute;left:0;right:0;top:0;will-change:transform;text-align:left;
  transition:transform var(--st) cubic-bezier(.2,.8,.2,1)}
.line{margin:0;padding:.28em .1em;font-size:calc(var(--ls)*clamp(24px,3.2vw,58px));
  font-weight:800;line-height:1.18;letter-spacing:-.01em;overflow-wrap:break-word;
  cursor:pointer;opacity:.28;filter:blur(3px);transform:scale(.945);
  transform-origin:left center;text-shadow:0 1px 14px rgba(0,0,0,.4);
  transition:opacity var(--lt) ease,filter var(--lt) ease,
             transform var(--lt) cubic-bezier(.2,.8,.2,1)}
.line.active{opacity:1;transform:scale(1);filter:drop-shadow(0 0 18px var(--glow))}
.line.gap{letter-spacing:.32em}
body[data-align="center"] #track{text-align:center}
body[data-align="center"] .line{transform-origin:center center}
.msg{position:absolute;top:42%;left:0;right:0;font-size:clamp(19px,2.1vw,32px);
  font-weight:700;opacity:.55}

/* side card -------------------------------------------------------------- */
#side{flex:0 0 auto;display:flex;align-items:center;justify-content:center;max-width:46vw}
.card{width:calc(min(30vw,52vh)*var(--cs));max-width:100%}
#art{display:block;width:100%;aspect-ratio:1;object-fit:cover;border-radius:16px;
  background:#1b1b22;box-shadow:0 30px 80px rgba(0,0,0,.55)}
#art.none{visibility:hidden}
#meta{margin-top:18px;text-align:center;max-width:100%}
#title{font-size:calc(var(--ls)*clamp(18px,1.9vw,30px));font-weight:800;line-height:1.2;
  overflow-wrap:break-word}
#artist{margin-top:6px;font-size:clamp(13px,1.2vw,18px);opacity:.7}
body[data-meta="0"] #meta,body[data-meta="0"] #coverMeta{display:none}

/* full-cover layout ------------------------------------------------------ */
#coverMeta{position:absolute;top:clamp(18px,5vh,54px);left:clamp(18px,5vw,64px);
  z-index:6;display:none;max-width:60vw;text-shadow:0 2px 24px rgba(0,0,0,.75)}
#ctitle{font-size:clamp(18px,2.1vw,32px);font-weight:800;line-height:1.15}
#cartist{opacity:.72;font-size:clamp(12px,1.2vw,17px);margin-top:4px}
body[data-layout="cover"] #bg{inset:0;filter:brightness(.6) saturate(1.05) blur(1px);
  transform:scale(1.05)}
body[data-layout="cover"][data-bg="liquid"] #bg{
  inset:-12%;filter:blur(62px) brightness(.4) saturate(1.35);transform:scale(1.03)}
body[data-layout="cover"][data-bg="colors"] #bg,
body[data-layout="cover"][data-bg="dark"] #bg{filter:none;transform:none}
body[data-layout="cover"] #shade{
  background:linear-gradient(180deg,rgba(0,0,0,.58),rgba(0,0,0,.06) 40%,rgba(0,0,0,.9))}
body[data-layout="cover"] #stage{display:block;padding:0}
body[data-layout="cover"] #side{display:none}
body[data-layout="cover"] #lyricsWrap{position:absolute;left:0;right:0;bottom:0;
  height:64vh;display:block;padding:0 clamp(18px,6vw,90px) 10vh}
body[data-layout="cover"] #lyrics{height:100%}
body[data-layout="cover"] #coverMeta{display:block}

/* layout variants -------------------------------------------------------- */
body[data-layout="rl"] #stage{flex-direction:row-reverse}
body[data-layout="lyrics"] #stage{justify-content:center}
body[data-layout="lyrics"] #side{display:none}
body[data-layout="lyrics"] #lyricsWrap{max-width:76ch;width:100%}
body[data-layout="stack"] #stage{flex-direction:column;justify-content:center;
  gap:2.4vh;padding-top:4vh;padding-bottom:15vh}
body[data-layout="stack"] #side{order:-1}
body[data-layout="stack"] #lyricsWrap{height:auto;flex:1 1 auto;width:100%;min-height:0}
body[data-layout="stack"] .card{width:calc(min(46vw,24vh)*var(--cs))}

/* animation levels ------------------------------------------------------- */
body[data-anim="subtle"]{--lt:.4s;--st:.55s}
body[data-anim="normal"]{--lt:.55s;--st:.72s}
body[data-anim="cinematic"]{--lt:.85s;--st:1s}
body[data-anim="cinematic"] .line.active{
  animation:pop .9s cubic-bezier(.2,.8,.2,1);
  filter:drop-shadow(0 0 22px var(--glow)) drop-shadow(0 0 64px var(--glow))}
body[data-anim="off"]{--lt:0s;--st:0s}
body[data-anim="off"] .line.active{animation:none}
@keyframes pop{0%{transform:scale(.98)}60%{transform:scale(1.005)}100%{transform:scale(1)}}

/* clock ------------------------------------------------------------------ */
#clock{position:fixed;top:18px;left:50%;transform:translateX(-50%);z-index:7;
  font-size:13px;letter-spacing:.06em;font-variant-numeric:tabular-nums;
  opacity:.45;display:none}
body[data-clock="1"] #clock{display:block}

/* controls --------------------------------------------------------------- */
#controls{position:fixed;left:50%;bottom:calc(16px + env(safe-area-inset-bottom));
  transform:translate(-50%,14px);z-index:30;display:flex;align-items:center;
  gap:clamp(6px,1vw,14px);width:min(780px,calc(100vw - 24px));
  padding:10px 14px;border-radius:20px;background:rgba(16,16,22,.55);
  border:1px solid rgba(255,255,255,.1);
  -webkit-backdrop-filter:blur(18px) saturate(1.3);
  backdrop-filter:blur(18px) saturate(1.3);
  box-shadow:0 18px 50px rgba(0,0,0,.45);
  opacity:0;pointer-events:none;
  transition:opacity .35s,transform .35s}
body.ui #controls{opacity:1;transform:translate(-50%,0);pointer-events:auto}
body[data-bar="0"] .seek{display:none}
body[data-bar="0"] #controls{justify-content:center}
.iconbtn{width:40px;height:40px;min-width:40px;border:0;border-radius:50%;
  background:rgba(255,255,255,.09);color:#fff;display:grid;place-items:center;
  cursor:pointer;transition:background .2s,transform .2s,opacity .25s}
.iconbtn:hover{background:rgba(255,255,255,.2)}
.iconbtn:active{transform:scale(.93)}
.iconbtn svg{width:20px;height:20px;fill:currentColor;display:block}
#play{width:46px;height:46px;background:rgba(255,255,255,.92);color:#101014}
#play .i-pause{display:none}
body[data-playing="1"] #play .i-play{display:none}
body[data-playing="1"] #play .i-pause{display:block}
.seek{display:flex;align-items:center;gap:10px;flex:1;min-width:0;
  font-size:12.5px;font-variant-numeric:tabular-nums;opacity:.9}
.seek span{min-width:34px;text-align:center}
.seek span:first-child{text-align:right}
.bar{position:relative;flex:1;min-width:40px;height:24px;display:flex;align-items:center;
  cursor:pointer;touch-action:none}
.bar::before{content:"";position:absolute;left:0;right:0;height:5px;border-radius:99px;
  background:rgba(255,255,255,.22)}
#fill{position:absolute;left:0;height:5px;width:0;border-radius:99px;background:#fff;
  pointer-events:none}
#fill::after{content:"";position:absolute;right:-6px;top:50%;width:12px;height:12px;
  border-radius:50%;background:#fff;transform:translateY(-50%);
  box-shadow:0 0 10px rgba(0,0,0,.45)}

/* settings gear (hidden until hover / tap) ------------------------------- */
#gearHot{position:fixed;top:0;right:0;width:110px;height:110px;z-index:45;
  display:flex;justify-content:flex-end;align-items:flex-start;padding:14px}
#gear{width:42px;height:42px;opacity:0;transform:translateY(-8px) scale(.9);
  pointer-events:none;background:rgba(24,24,30,.6);
  -webkit-backdrop-filter:blur(12px);backdrop-filter:blur(12px)}
body.ui #gear,body.settings #gear{opacity:.5;pointer-events:auto;transform:none}
#gearHot:hover #gear,#gear:focus-visible,body.settings #gear{
  opacity:1;pointer-events:auto;transform:none}

/* settings sheet --------------------------------------------------------- */
#settings{position:fixed;inset:0;z-index:60;display:flex;justify-content:flex-end;
  background:rgba(0,0,0,0);pointer-events:none;transition:background .3s}
body.settings #settings{background:rgba(0,0,0,.4);pointer-events:auto}
.sheet{width:min(400px,88vw);height:100%;background:rgba(19,19,25,.94);
  border-left:1px solid rgba(255,255,255,.1);
  -webkit-backdrop-filter:blur(24px);backdrop-filter:blur(24px);
  padding:18px 20px calc(26px + env(safe-area-inset-bottom));overflow-y:auto;
  transform:translateX(100%);transition:transform .35s cubic-bezier(.2,.8,.2,1);
  -webkit-overflow-scrolling:touch}
body.settings .sheet{transform:none}
.shead{display:flex;align-items:center;justify-content:space-between;
  position:sticky;top:-18px;background:rgba(19,19,25,.96);padding:12px 0;
  margin:-12px 0 6px;z-index:2}
.shead h2{margin:0;font-size:17px;letter-spacing:.02em}
.grp{margin:20px 0}
.grp>label{display:block;font-size:11px;letter-spacing:.14em;text-transform:uppercase;
  opacity:.55;margin-bottom:9px}
.seg{display:flex;flex-wrap:wrap;gap:6px}
.seg button{flex:1 0 auto;padding:9px 10px;border-radius:10px;
  border:1px solid rgba(255,255,255,.13);background:rgba(255,255,255,.05);
  color:#fff;font:inherit;font-size:13px;cursor:pointer;transition:.18s}
.seg button:hover{background:rgba(255,255,255,.12)}
.seg button.on{background:#fff;color:#101014;border-color:#fff;font-weight:700}
.srcrow{display:flex;gap:8px;align-items:center}
#source{flex:1;min-width:0;padding:10px 12px;border-radius:10px;
  border:1px solid rgba(255,255,255,.13);background:rgba(255,255,255,.05);
  color:#fff;font:inherit;font-size:13.5px;cursor:pointer}
#source option{background:#191920;color:#fff}
#srcRefresh{width:38px;height:38px;min-width:38px}
#srcHint,#bgHint{margin-top:8px}
input[type=range]{width:100%;accent-color:#fff;height:28px;cursor:pointer}
.row{display:flex;align-items:center;justify-content:space-between;gap:12px;
  padding:9px 0;font-size:14px}
.switch{position:relative;width:46px;height:27px;border-radius:99px;
  background:rgba(255,255,255,.18);border:0;cursor:pointer;transition:background .2s;
  flex:0 0 auto;padding:0}
.switch::after{content:"";position:absolute;top:3px;left:3px;width:21px;height:21px;
  border-radius:50%;background:#fff;transition:transform .2s}
.switch.on{background:#fff}
.switch.on::after{background:#101014;transform:translateX(19px)}
.wide{width:100%;margin-top:10px;padding:12px;border-radius:11px;
  border:1px solid rgba(255,255,255,.16);background:rgba(255,255,255,.07);
  color:#fff;font:inherit;font-size:14px;font-weight:600;cursor:pointer;transition:.18s}
.wide:hover{background:rgba(255,255,255,.14)}
.hint{font-size:11.5px;opacity:.42;line-height:1.5;margin-top:18px}

/* small / portrait screens ---------------------------------------------- */
@media (max-width:820px), (max-aspect-ratio:4/5){
  body[data-layout="lr"] #stage,body[data-layout="rl"] #stage{
    flex-direction:column;justify-content:center;gap:2.2vh;padding:3vh 5vw 15vh}
  body[data-layout="lr"] #side,body[data-layout="rl"] #side{order:-1}
  body[data-layout="lr"] #lyricsWrap,body[data-layout="rl"] #lyricsWrap{
    height:auto;flex:1 1 auto;width:100%;min-height:0}
  body[data-layout="lr"] .card,body[data-layout="rl"] .card{
    width:calc(min(44vw,22vh)*var(--cs))}
  body[data-layout="cover"] #lyricsWrap{height:58vh;padding:0 5vw 14vh}
  .line{font-size:calc(var(--ls)*clamp(21px,5vw,40px))}
  #title{font-size:clamp(15px,4vw,21px)}
  #artist{font-size:clamp(12px,3.2vw,16px)}
  #controls{padding:8px 10px;gap:6px;border-radius:18px}
  .iconbtn{width:38px;height:38px;min-width:38px}
  #play{width:44px;height:44px}
  .seek span{min-width:28px;font-size:11.5px}
}
@media (max-width:420px){
  .seek span{display:none}
}
</style>
</head>
<body data-layout="lr" data-bg="liquid" data-bg-motion="1" data-anim="normal" data-align="left"
      data-meta="1" data-clock="0" data-bar="1" data-playing="0">
<svg width="0" height="0" style="position:absolute" aria-hidden="true">
  <symbol id="i-play" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></symbol>
  <symbol id="i-pause" viewBox="0 0 24 24"><path d="M6 5h4v14H6zm8 0h4v14h-4z"/></symbol>
  <symbol id="i-prev" viewBox="0 0 24 24"><path d="M6 6h2v12H6zm12 0v12l-9-6z"/></symbol>
  <symbol id="i-next" viewBox="0 0 24 24"><path d="M16 6h2v12h-2zM6 6l9 6-9 6z"/></symbol>
  <symbol id="i-fs" viewBox="0 0 24 24"><path d="M4 9V4h5v2H6v3H4zm10-5h5v5h-2V6h-3V4zM4 15h2v3h3v2H4v-5zm14 0h2v5h-5v-2h3v-3z"/></symbol>
  <symbol id="i-close" viewBox="0 0 24 24"><path d="M6.4 5 5 6.4 10.6 12 5 17.6 6.4 19 12 13.4 17.6 19 19 17.6 13.4 12 19 6.4 17.6 5 12 10.6z"/></symbol>
  <symbol id="i-gear" viewBox="0 0 24 24"><path d="M19.43 12.98c.04-.32.07-.65.07-.98s-.03-.66-.08-.98l2.11-1.65c.19-.15.24-.42.12-.64l-2-3.46c-.12-.22-.39-.3-.61-.22l-2.49 1c-.52-.4-1.08-.73-1.69-.98l-.38-2.65C14.46 2.18 14.25 2 14 2h-4c-.25 0-.46.18-.49.42l-.38 2.65c-.61.25-1.17.59-1.69.98l-2.49-1c-.23-.09-.49 0-.61.22l-2 3.46c-.13.22-.07.49.12.64l2.11 1.65c-.04.32-.08.66-.08.98s.03.66.08.98l-2.11 1.65c-.19.15-.24.42-.12.64l2 3.46c.12.22.39.3.61.22l2.49-1c.52.4 1.08.73 1.69.98l.38 2.65c.03.24.24.42.49.42h4c.25 0 .46-.18.49-.42l.38-2.65c.61-.25 1.17-.59 1.69-.98l2.49 1c.23.09.49 0 .61-.22l2-3.46c.12-.22.07-.49-.12-.64l-2.11-1.65zM12 15.5A3.5 3.5 0 1 1 12 8.5a3.5 3.5 0 0 1 0 7z"/></symbol>
  <symbol id="i-refresh" viewBox="0 0 24 24"><path d="M17.65 6.35A8 8 0 1 0 19.73 14h-2.08A6 6 0 1 1 12 6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z"/></symbol>
</svg>

<div id="bg"></div>
<div id="liquid" aria-hidden="true">
  <div class="liquid-layer liquid-a"></div>
  <div class="liquid-layer liquid-b"></div>
  <div class="liquid-layer liquid-c"></div>
</div>
<div id="shade"></div>
<div id="clock">--:--</div>

<div id="stage">
  <div id="lyricsWrap"><div id="lyrics"><div id="track"></div></div></div>
  <aside id="side"><div class="card">
    <img id="art" class="none" alt="">
    <div id="meta"><div id="title">Nothing playing</div><div id="artist"></div></div>
  </div></aside>
  <div id="coverMeta"><div id="ctitle"></div><div id="cartist"></div></div>
</div>

<div id="controls">
  <button id="prev" class="iconbtn" type="button" aria-label="Previous track"><svg><use href="#i-prev"/></svg></button>
  <button id="play" class="iconbtn" type="button" aria-label="Play or pause">
    <svg class="i-play"><use href="#i-play"/></svg><svg class="i-pause"><use href="#i-pause"/></svg>
  </button>
  <button id="next" class="iconbtn" type="button" aria-label="Next track"><svg><use href="#i-next"/></svg></button>
  <div class="seek">
    <span id="cur">0:00</span>
    <div class="bar" id="bar"><div id="fill"></div></div>
    <span id="tot">0:00</span>
  </div>
  <button id="fs" class="iconbtn" type="button" aria-label="Toggle fullscreen"><svg><use href="#i-fs"/></svg></button>
</div>

<div id="gearHot">
  <button id="gear" class="iconbtn" type="button" aria-label="Settings"><svg><use href="#i-gear"/></svg></button>
</div>

<div id="settings" aria-hidden="true">
  <div class="sheet" role="dialog" aria-modal="true" aria-label="Settings">
    <div class="shead"><h2>Settings</h2>
      <button id="close" class="iconbtn" type="button" aria-label="Close settings"><svg><use href="#i-close"/></svg></button>
    </div>

    <div class="grp"><label>Lyrics &amp; cover position</label>
      <div class="seg" data-key="layout">
        <button type="button" data-v="lr">Left</button>
        <button type="button" data-v="rl">Right</button>
        <button type="button" data-v="stack">Stacked</button>
        <button type="button" data-v="lyrics">Lyrics only</button>
        <button type="button" data-v="cover">Full cover</button>
      </div>
    </div>

    <div class="grp"><label>Lyrics alignment</label>
      <div class="seg" data-key="align">
        <button type="button" data-v="left">Left</button>
        <button type="button" data-v="center">Center</button>
      </div>
    </div>

    <div class="grp"><label>Animation</label>
      <div class="seg" data-key="anim">
        <button type="button" data-v="off">Off</button>
        <button type="button" data-v="subtle">Subtle</button>
        <button type="button" data-v="normal">Normal</button>
        <button type="button" data-v="cinematic">Cinematic</button>
      </div>
    </div>

    <div class="grp"><label>Background</label>
      <div class="seg" data-key="bg">
        <button type="button" data-v="liquid">Liquid</button>
        <button type="button" data-v="blur">Blur</button>
        <button type="button" data-v="colors">Colors</button>
        <button type="button" data-v="dark">Dark</button>
      </div>
      <p class="hint" id="bgHint">Liquid blends the blurred cover into a flowing background.</p>
    </div>

    <div class="grp"><label>Audio source</label>
      <div class="srcrow">
        <select id="source" aria-label="Audio source"></select>
        <button id="srcRefresh" class="iconbtn" type="button" aria-label="Refresh audio sources">
          <svg><use href="#i-refresh"/></svg>
        </button>
      </div>
      <p class="hint" id="srcHint">Apps currently open on this computer.</p>
    </div>

    <div class="grp"><label>Text size</label>
      <input type="range" data-key="ls" min="0.7" max="1.6" step="0.05" aria-label="Text size">
    </div>

    <div class="grp"><label>Cover size</label>
      <input type="range" data-key="cs" min="0.6" max="1.5" step="0.05" aria-label="Cover size">
    </div>

    <div class="grp"><label>Display</label>
      <div class="row"><span>Song title &amp; artist</span><button class="switch" type="button" data-key="meta" aria-label="Show song info"></button></div>
      <div class="row"><span>Seek bar</span><button class="switch" type="button" data-key="bar" aria-label="Show seek bar"></button></div>
      <div class="row"><span>Clock</span><button class="switch" type="button" data-key="clock" aria-label="Show clock"></button></div>
    </div>

    <div class="grp">
      <button id="fsBtn" class="wide" type="button">Toggle fullscreen</button>
      <button id="reset" class="wide" type="button">Reset to defaults</button>
    </div>

    <p class="hint">Move the mouse or tap anywhere to show controls. Swipe right on the lyrics for the next track, left for the previous one. Tap the top-right corner for this panel. Layout settings are saved on this device; the audio source is shared by the server.</p>
  </div>
</div>

<script>
const $ = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));
const body = document.body;
const LS_KEY = 'verselink.settings.v2';
const LEGACY_LS_KEYS = ['verselink.settings.v1', 'synclyrics.settings.v2'];
const DEFAULTS = {layout:'lr', align:'left', ls:1, cs:1, anim:'normal',
                  bg:'liquid', meta:true, bar:true, clock:false};
let S = Object.assign({}, DEFAULTS);
try {
  let raw = localStorage.getItem(LS_KEY), migrated = false;
  if (!raw) {
    for (const key of LEGACY_LS_KEYS) {
      raw = localStorage.getItem(key);
      if (raw) { migrated = true; break; }
    }
  }
  const saved = JSON.parse(raw || '{}');
  S = Object.assign(S, saved);
  // Replace the old default and removed mode without resetting other preferences.
  if (migrated && (saved.bg === 'blur' || saved.bg === 'viz')) S.bg = 'liquid';
  if (!['liquid', 'blur', 'colors', 'dark'].includes(S.bg)) S.bg = 'liquid';
  if (migrated) localStorage.setItem(LS_KEY, JSON.stringify(S));
} catch (e) {}
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');

let st = {status:'none', dur:0, pos:0}, syncAt = performance.now();
let lines = [], curId = null, active = -2;
let dragging = false, dragT = 0;
let uiWasVisible = false;
let lastSwipeAt = 0;
let srcTimer = null;

const fmt = s => {
  s = Math.max(0, Math.floor(s || 0));
  return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
};
const post = p => fetch(p, {method:'POST',
  headers:{'X-Requested-With':'verselink'}}).catch(() => {});

/* ------------------------------------------------------------- settings -- */
function saveSettings() {
  try { localStorage.setItem(LS_KEY, JSON.stringify(S)); } catch (e) {}
}
function applySettings() {
  body.dataset.layout = S.layout;
  body.dataset.align = S.align;
  body.dataset.anim = S.anim;
  body.dataset.bg = S.bg;
  body.dataset.meta = S.meta ? '1' : '0';
  body.dataset.bar = S.bar ? '1' : '0';
  body.dataset.clock = S.clock ? '1' : '0';
  const root = document.documentElement.style;
  root.setProperty('--ls', S.ls);
  root.setProperty('--cs', S.cs);
  $$('.seg').forEach(seg => {
    const key = seg.dataset.key;
    seg.querySelectorAll('button').forEach(b =>
      b.classList.toggle('on', String(S[key]) === b.dataset.v));
  });
  $$('.switch').forEach(sw => sw.classList.toggle('on', !!S[sw.dataset.key]));
  $$('input[type=range]').forEach(r => {
    if (r.dataset.key in S) r.value = S[r.dataset.key];
  });
  requestAnimationFrame(layout);
  syncBackground();
}

/* ------------------------------------------------------- background motion -- */
function syncBackground() {
  body.dataset.bgMotion = !document.hidden && S.anim !== 'off' && !reducedMotion.matches ? '1' : '0';
  $('#bgHint').textContent = S.bg !== 'liquid'
    ? 'Choose Liquid for a flowing blur of the album cover.'
    : reducedMotion.matches ? 'Liquid is still because your device requests reduced motion.'
    : S.anim === 'off' ? 'Choose Normal, Subtle, or Cinematic under Animation to enable motion.'
    : 'Liquid blends the blurred cover into a flowing background.';
}
document.addEventListener('visibilitychange', syncBackground);
if (reducedMotion.addEventListener) reducedMotion.addEventListener('change', syncBackground);
else reducedMotion.addListener(syncBackground);

/* -------------------------------------------------------------- sources -- */
function sourceOption(p) {
  return p.name === p.id ? p.id : p.name + '  \u00b7  ' + p.id;
}
async function loadSources() {
  const sel = $('#source'), hint = $('#srcHint');
  try {
    const j = await (await fetch('/players', {cache:'no-store'})).json();
    const players = j.players || [];
    const active = j.active || '';
    const sig = JSON.stringify([players.map(p => [p.id, p.name]), active]);
    if (sel.dataset.sig !== sig) {
      sel.dataset.sig = sig;
      sel.innerHTML = '';
      const auto = document.createElement('option');
      auto.value = '';
      auto.textContent = 'Automatic (first playing app)';
      sel.appendChild(auto);
      players.forEach(p => {
        const o = document.createElement('option');
        o.value = p.id;
        o.textContent = sourceOption(p);
        sel.appendChild(o);
      });
      sel.value = active;
      if (sel.value !== active) sel.value = '';
    }
    hint.textContent = players.length
      ? players.length + ' app' + (players.length === 1 ? '' : 's') +
        ' detected. Pick one, or keep Automatic.'
      : 'No media apps detected. Start playback in Spotify, VLC, a browser, or another player.';
  } catch (e) {
    hint.textContent = 'Could not read the list of media apps.';
  }
}
function watchSources(on) {
  clearInterval(srcTimer);
  srcTimer = null;
  if (on) {
    loadSources();
    srcTimer = setInterval(loadSources, 3000);
  }
}
$('#srcRefresh').onclick = loadSources;
$('#source').addEventListener('change', async () => {
  const pid = $('#source').value;
  try {
    const r = await fetch('/player?p=' + encodeURIComponent(pid), {
      method: 'POST', headers: {'X-Requested-With': 'verselink'}});
    if (r.ok) {
      curId = null;
      lines = [];
      active = -2;
      showMsg(pid ? 'Switching audio source\u2026' : 'Using the first playing app\u2026');
    }
  } catch (e) {}
  loadSources();
});

/* ------------------------------------------------------- reveal on idle -- */
let idleT;
function showUI() {
  body.classList.remove('idle');
  if (!body.classList.contains('settings')) body.classList.add('ui');
  clearTimeout(idleT);
  idleT = setTimeout(() => {
    if (body.classList.contains('settings')) return;
    body.classList.remove('ui');
    body.classList.add('idle');
  }, 3200);
}
function openSettings() {
  body.classList.add('settings', 'ui');
  $('#settings').setAttribute('aria-hidden', 'false');
  clearTimeout(idleT);
  watchSources(true);
}
function closeSettings() {
  body.classList.remove('settings');
  $('#settings').setAttribute('aria-hidden', 'true');
  watchSources(false);
  showUI();
}

/* ---------------------------------------------------------------- lyrics -- */
function showMsg(text) {
  lines = []; active = -2;
  const tr = $('#track');
  tr.style.transform = 'translateY(0)';
  tr.innerHTML = '';
  const d = document.createElement('div');
  d.className = 'msg';
  d.textContent = text;
  tr.appendChild(d);
}

function buildLines() {
  const tr = $('#track');
  tr.innerHTML = '';
  active = -2;
  lines.forEach(l => {
    const d = document.createElement('div');
    d.className = 'line' + (l[1] ? '' : ' gap');
    d.textContent = l[1] || '\u266A';
    d.addEventListener('click', () => {
      // While the controls are hidden, the first click/tap only brings them back.
      if (!uiWasVisible) { showUI(); return; }
      if (performance.now() - lastSwipeAt < 500) return;  // tail of a swipe
      seek(l[0]);
    });
    tr.appendChild(d);
  });
  layout();
}

function layout() {
  const tr = $('#track'), els = tr.children;
  if (!lines.length || !els.length) return;
  for (let k = 0; k < els.length; k++) {
    const d = Math.abs(k - active), el = els[k];
    el.classList.toggle('active', k === active);
    el.style.filter = k === active ? '' : 'blur(' + Math.min(1 + d * 1.05, 7) + 'px)';
    el.style.opacity = k === active ? '' : String(Math.max(0.14, 0.5 - d * 0.09));
  }
  const el = els[Math.min(Math.max(active, 0), els.length - 1)];
  const frac = body.dataset.layout === 'cover' ? 0.52 : 0.45;
  const y = el.offsetTop + el.offsetHeight / 2;
  tr.style.transform =
    'translateY(' + ($('#lyrics').clientHeight * frac - y) + 'px)';
}

/* ------------------------------------------------------------------- art -- */
function applyPalette(c1, c2) {
  const root = document.documentElement.style;
  root.setProperty('--c1', 'rgb(' + c1 + ')');
  root.setProperty('--c2', 'rgb(' + c2 + ')');
  root.setProperty('--c1a', 'rgba(' + c1 + ',.75)');
  root.setProperty('--c2a', 'rgba(' + c2 + ',.75)');
  root.setProperty('--glow', 'rgba(' + c1 + ',.38)');
}

function palette(img) {
  try {
    const c = document.createElement('canvas');
    c.width = c.height = 32;
    const x = c.getContext('2d', {willReadFrequently:true});
    x.drawImage(img, 0, 0, 32, 32);
    const d = x.getImageData(0, 0, 32, 32).data, bins = new Map();
    for (let i = 0; i < d.length; i += 4) {
      const r = d[i], g = d[i+1], b = d[i+2];
      const mx = Math.max(r, g, b), mn = Math.min(r, g, b);
      if (mx < 30 || mx - mn < 16) continue;
      const k = ((r >> 4) << 8) | ((g >> 4) << 4) | (b >> 4);
      let o = bins.get(k);
      if (!o) { o = {n:0, r:0, g:0, b:0}; bins.set(k, o); }
      o.n++; o.r += r; o.g += g; o.b += b;
    }
    const top = [...bins.values()].sort((a, b) => b.n - a.n).slice(0, 2);
    const pick = (i, fb) => top[i]
      ? [Math.round(top[i].r / top[i].n), Math.round(top[i].g / top[i].n),
         Math.round(top[i].b / top[i].n)]
      : fb;
    const lift = v => {
      const m = Math.max(v[0], v[1], v[2]) || 1;
      const f = Math.min(1.9, 150 / m);
      return [Math.round(v[0] * f), Math.round(v[1] * f), Math.round(v[2] * f)];
    };
    const c1 = lift(pick(0, [64, 104, 150]));
    applyPalette(c1, lift(pick(1, c1)));
  } catch (e) {}
}

function prepareLiquidArt(img) {
  palette(img);
  // Prepare a small, soft texture once per cover; animation only moves CSS layers.
  try {
    const c = document.createElement('canvas');
    c.width = c.height = 96;
    const x = c.getContext('2d');
    x.filter = 'blur(5px)';
    x.drawImage(img, -12, -12, 120, 120);
    document.documentElement.style.setProperty('--liquid-art', 'url("' + c.toDataURL('image/jpeg', .8) + '")');
  } catch (e) {}
}

function clearArtBackground() {
  const root = document.documentElement.style;
  root.setProperty('--art', 'none');
  root.setProperty('--liquid-art', 'none');
  applyPalette([64, 104, 150], [140, 88, 158]);
}

function setArt(src) {
  const img = $('#art'), root = document.documentElement.style;
  if (src) {
    img.classList.remove('none');
    if (img.getAttribute('src') !== src) {
      root.setProperty('--art', 'url("' + src + '")');
      root.setProperty('--liquid-art', 'url("' + src + '")');
      img.onload = () => prepareLiquidArt(img);
      img.onerror = () => {
        img.classList.add('none');
        clearArtBackground();
      };
      img.src = src;
    }
    if (img.complete && img.naturalWidth) prepareLiquidArt(img);
  } else {
    img.classList.add('none');
    img.removeAttribute('src');
    img.onload = img.onerror = null;
    clearArtBackground();
  }
}

/* ------------------------------------------------------------- playback -- */
function nowPos() {
  if (st.status === 'none') return 0;
  return st.status === 'Playing'
    ? (st.pos || 0) + (performance.now() - syncAt) / 1000
    : (st.pos || 0);
}

async function onTrack(s) {
  $('#title').textContent = s.title || 'Unknown';
  $('#artist').textContent = s.artist || '';
  $('#ctitle').textContent = s.title || '';
  $('#cartist').textContent = s.artist || '';
  document.title = (s.title || 'VerseLink') + (s.artist ? ' \u2014 ' + s.artist : '');
  setArt(s.art);
  showMsg('Loading lyrics\u2026');
  try {
    const j = await (await fetch('/lyrics', {cache:'no-store'})).json();
    if (j.id !== curId) return;
    if (!j.lines || !j.lines.length) {
      showMsg('No synced lyrics for this song');
      return;
    }
    lines = j.lines;
    buildLines();
  } catch (e) {
    showMsg('Could not load lyrics');
  }
}

async function poll() {
  try {
    const s = await (await fetch('/state', {cache:'no-store'})).json();
    st = s;
    syncAt = performance.now();
    body.dataset.playing = s.status === 'Playing' ? '1' : '0';
    $('#tot').textContent = fmt(s.dur || 0);
    if (s.status === 'none') {
      if (curId !== 'none') {
        curId = 'none';
        setArt('');
        showMsg('Nothing playing');
        $('#title').textContent = 'Nothing playing';
        $('#artist').textContent = '';
        $('#ctitle').textContent = '';
        $('#cartist').textContent = '';
        document.title = 'VerseLink';
      }
    } else if (s.id !== curId) {
      curId = s.id;
      onTrack(s);
    }
  } catch (e) {}
  setTimeout(poll, 400);
}

/* ----------------------------------------------------------------- frame -- */
function paintBar(t) {
  $('#fill').style.width = (st.dur ? Math.min(100, t / st.dur * 100) : 0) + '%';
  $('#cur').textContent = fmt(t);
}

function frame() {
  const t = nowPos();
  if (!dragging) paintBar(t);
  if (lines.length) {
    let i = -1;
    for (let k = 0; k < lines.length; k++) {
      if (lines[k][0] <= t + 0.12) i = k; else break;
    }
    if (i !== active) { active = i; layout(); }
  }
  requestAnimationFrame(frame);
}

/* -------------------------------------------------------------- controls -- */
function seek(t) {
  t = Math.max(0, t || 0);
  post('/seek?t=' + t.toFixed(3));
  st.pos = t;
  syncAt = performance.now();
  paintBar(t);
  showUI();
}

const barEl = $('#bar');
function barTime(e) {
  const r = barEl.getBoundingClientRect();
  const f = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
  return f * (st.dur || 0);
}
barEl.addEventListener('pointerdown', e => {
  if (!st.dur) return;
  e.preventDefault();
  dragging = true;
  dragT = barTime(e);
  try { barEl.setPointerCapture(e.pointerId); } catch (err) {}
  paintBar(dragT);
});
barEl.addEventListener('pointermove', e => {
  if (!dragging) return;
  dragT = barTime(e);
  paintBar(dragT);
});
barEl.addEventListener('pointerup', () => {
  if (!dragging) return;
  dragging = false;
  seek(dragT);
});
barEl.addEventListener('pointercancel', () => { dragging = false; });

$('#play').onclick = () => {
  if (st.status === 'none') return;
  const wasPlaying = st.status === 'Playing';
  const t = nowPos();
  post('/cmd?c=play-pause');
  st.pos = t;
  st.status = wasPlaying ? 'Paused' : 'Playing';
  syncAt = performance.now();
  body.dataset.playing = wasPlaying ? '0' : '1';
  showUI();
};
$('#prev').onclick = () => { post('/cmd?c=previous'); showUI(); };
$('#next').onclick = () => { post('/cmd?c=next'); showUI(); };

function toggleFs() {
  if (document.fullscreenElement) {
    document.exitFullscreen().catch(() => {});
  } else if (document.documentElement.requestFullscreen) {
    document.documentElement.requestFullscreen().catch(() => {});
  }
  showUI();
}
$('#fs').onclick = toggleFs;
$('#fsBtn').onclick = toggleFs;

/* --------------------------------------------------------------- events -- */
addEventListener('pointermove', e => {
  if (e.pointerType === 'mouse') showUI();
});
addEventListener('pointerdown', e => {
  uiWasVisible = body.classList.contains('ui');
  showUI();
}, true);
addEventListener('keydown', e => {
  const tag = (e.target.tagName || '').toLowerCase();
  const typing = tag === 'input' || tag === 'button' || tag === 'select' ||
                 tag === 'textarea';
  if (typing) {
    if (e.key === 'Escape') closeSettings();
    return;
  }
  const k = e.key;
  if (k === 'f' || k === 'F') toggleFs();
  else if (k === ' ') { e.preventDefault(); $('#play').click(); }
  else if (k === 'ArrowRight') post('/cmd?c=next');
  else if (k === 'ArrowLeft') post('/cmd?c=previous');
  else if (k === 's' || k === 'S') {
    body.classList.contains('settings') ? closeSettings() : openSettings();
  } else if (k === 'Escape') closeSettings();
  showUI();
});

$$('.seg').forEach(seg => seg.addEventListener('click', e => {
  const b = e.target.closest('button');
  if (!b || !b.dataset.v) return;
  S[seg.dataset.key] = b.dataset.v;
  applySettings();
  saveSettings();
}));
$$('.switch').forEach(sw => sw.addEventListener('click', () => {
  S[sw.dataset.key] = !S[sw.dataset.key];
  applySettings();
  saveSettings();
}));
$$('input[type=range]').forEach(r => r.addEventListener('input', () => {
  S[r.dataset.key] = parseFloat(r.value);
  applySettings();
  saveSettings();
}));
$('#gear').onclick = openSettings;
$('#gearHot').addEventListener('click', () => {
  if (!body.classList.contains('settings')) openSettings();
});
$('#close').onclick = closeSettings;
$('#settings').addEventListener('click', e => {
  if (e.target.id === 'settings') closeSettings();
});
$('#reset').onclick = () => {
  S = Object.assign({}, DEFAULTS);
  applySettings();
  saveSettings();
};

/* ------------------------------------------------------------- gestures -- */
/* Swipe right on the lyrics = next track, swipe left = previous track. */
const stageEl = $('#stage');
const SWIPE_MIN = 56;
let swipe = null;

stageEl.addEventListener('touchstart', e => {
  if (body.classList.contains('settings') || e.touches.length !== 1) {
    swipe = null;
    return;
  }
  const t = e.touches[0];
  swipe = {x: t.clientX, y: t.clientY, t: performance.now()};
}, {passive: true});

stageEl.addEventListener('touchmove', e => {
  if (!swipe || body.classList.contains('settings')) return;
  const t = e.touches[0];
  const dx = t.clientX - swipe.x, dy = t.clientY - swipe.y;
  // clearly horizontal: stop the browser from reading it as a pan
  if (Math.abs(dx) > 16 && Math.abs(dx) > Math.abs(dy) * 1.2) e.preventDefault();
}, {passive: false});

function finishSwipe(e, cancelled) {
  if (!swipe) return;
  const s = swipe;
  swipe = null;
  if (cancelled || body.classList.contains('settings')) return;
  const t = e.changedTouches[0];
  const dx = t.clientX - s.x, dy = t.clientY - s.y;
  const dt = performance.now() - s.t;
  const far = Math.abs(dx) >= SWIPE_MIN;
  const flick = Math.abs(dx) >= 34 && dt < 420;
  if ((!far && !flick) || Math.abs(dx) < Math.abs(dy) * 1.4) return;
  lastSwipeAt = performance.now();
  post(dx > 0 ? '/cmd?c=next' : '/cmd?c=previous');
  showUI();
}
stageEl.addEventListener('touchend', e => finishSwipe(e, false), {passive: true});
stageEl.addEventListener('touchcancel', () => { swipe = null; }, {passive: true});

addEventListener('resize', layout);
document.addEventListener('fullscreenchange', () => setTimeout(layout, 150));

function tickClock() {
  const d = new Date();
  $('#clock').textContent =
    String(d.getHours()).padStart(2, '0') + ':' +
    String(d.getMinutes()).padStart(2, '0');
}
setInterval(tickClock, 15000);
tickClock();

/* ------------------------------------------------------------------ init -- */
applySettings();
showMsg('Nothing playing');
poll();
requestAnimationFrame(frame);
showUI();
if (location.hash === '#settings') openSettings();
</script>
</body>
</html>
"""


def lan_addresses():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # no packet is sent; just picks a route
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if ip and not ip.startswith("127."))


def main():
    global PLAYER
    ap = argparse.ArgumentParser(description="Fullscreen synced lyrics for desktop media players")
    ap.add_argument("--player", help="initial source id, e.g. spotify or strawberry (default: automatic)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="0.0.0.0",
                    help="bind address (default: 0.0.0.0 = all interfaces)")
    args = ap.parse_args()
    set_player(args.player)

    try:
        srv = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as e:
        raise SystemExit(f"cannot listen on {args.host}:{args.port} ({e})")
    srv.daemon_threads = True

    shown = "localhost" if args.host in ("0.0.0.0", "::", "") else args.host
    print(f"VerseLink running: http://{shown}:{args.port}  (Ctrl+C to stop)", flush=True)
    if args.host in ("0.0.0.0", "::", ""):
        for ip in lan_addresses():
            print(f"  on your tablet: http://{ip}:{args.port}", flush=True)
        print("  (anyone on this network can control the player; "
              "use --host 127.0.0.1 to stay local)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
