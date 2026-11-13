#!/usr/bin/env python3
"""
AutoLyricEmbedder - fetch synced (timestamped) lyrics from LRCLIB and embed
them into the audio files in a directory.

Reads title / artist / album / duration from each file's tags, looks the song
up on https://lrclib.net, and writes the lyrics back into the file's metadata:

  MP3 / WAV / AIFF  ->  ID3 USLT (LRC text) + optional ID3 SYLT (true synced frame)
  FLAC / OGG / Opus ->  LYRICS Vorbis comment
  M4A / MP4 / ALAC  ->  (c)lyr atom
  WMA               ->  WM/Lyrics

Requires:  pip install -r requirements.txt   (mutagen, rich)
Usage:     python autolyricembedder.py [directory] [options]
           (run with no arguments for fully interactive mode)
"""

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    import mutagen
    from mutagen.asf import ASF
    from mutagen.id3 import ID3, SYLT, USLT, ID3NoHeaderError
    from mutagen.mp3 import MP3
    from mutagen.mp4 import MP4
    from rich import box
    from rich.align import Align
    from rich.console import Console, Group
    from rich.markup import escape
    from rich.panel import Panel
    from rich.progress import (BarColumn, MofNCompleteColumn, Progress,
                               SpinnerColumn, TextColumn, TimeElapsedColumn)
    from rich.prompt import Confirm, Prompt
    from rich.table import Table
    from rich.text import Text
except ImportError as _e:
    print("Missing dependency ({}). Install them with:\n"
          "    pip install -r requirements.txt".format(_e))
    sys.exit(1)

# Make sure non-ASCII titles/lyrics print on Windows consoles.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

console = Console(highlight=False)

API_BASE = "https://lrclib.net/api"
APP_NAME = "AutoLyricEmbedder v1.2"
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".autolyricembedder.json")
MIN_REQUEST_GAP = 0.3  # seconds between API calls (docs recommend 200-500 ms)

# Client identification (see docs/lrclibdocs.txt -> "Client identification").
# A User-Agent naming the app gets ~90 req/min; a registered App ID ~180 req/min.
CLIENT = {"user_agent": APP_NAME, "app_id": "", "api_key": ""}
_last_request = [0.0]
AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".mp4", ".m4b", ".alac", ".ogg",
              ".oga", ".opus", ".wav", ".aif", ".aiff", ".wma"}
LRC_LINE = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")
MAX_CHOICES = 8


# --------------------------------------------------------------------------- #
# Banner
# --------------------------------------------------------------------------- #
GLYPHS = {
    "A": [" █████╗ ", "██╔══██╗", "███████║", "██╔══██║", "██║  ██║", "╚═╝  ╚═╝"],
    "B": ["██████╗ ", "██╔══██╗", "██████╔╝", "██╔══██╗", "██████╔╝", "╚═════╝ "],
    "C": [" ██████╗", "██╔════╝", "██║     ", "██║     ", "╚██████╗", " ╚═════╝"],
    "D": ["██████╗ ", "██╔══██╗", "██║  ██║", "██║  ██║", "██████╔╝", "╚═════╝ "],
    "E": ["███████╗", "██╔════╝", "█████╗  ", "██╔══╝  ", "███████╗", "╚══════╝"],
    "I": ["██╗", "██║", "██║", "██║", "██║", "╚═╝"],
    "L": ["██╗     ", "██║     ", "██║     ", "██║     ", "███████╗", "╚══════╝"],
    "M": ["███╗   ███╗", "████╗ ████║", "██╔████╔██║", "██║╚██╔╝██║",
          "██║ ╚═╝ ██║", "╚═╝     ╚═╝"],
    "O": [" ██████╗ ", "██╔═══██╗", "██║   ██║", "██║   ██║", "╚██████╔╝", " ╚═════╝ "],
    "R": ["██████╗ ", "██╔══██╗", "██████╔╝", "██╔══██╗", "██║  ██║", "╚═╝  ╚═╝"],
    "T": ["████████╗", "╚══██╔══╝", "   ██║   ", "   ██║   ", "   ██║   ", "   ╚═╝   "],
    "U": ["██╗   ██╗", "██║   ██║", "██║   ██║", "██║   ██║", "╚██████╔╝", " ╚═════╝ "],
    "Y": ["██╗   ██╗", "╚██╗ ██╔╝", " ╚████╔╝ ", "  ╚██╔╝  ", "   ██║   ", "   ╚═╝   "],
}


def render_word(word):
    rows = []
    for i in range(6):
        width = lambda c: max(len(r) for r in GLYPHS[c])  # noqa: E731
        rows.append("".join(GLYPHS[c][i].ljust(width(c)) for c in word))
    return rows


def gradient(start, end, steps):
    s = [int(start[i:i + 2], 16) for i in (1, 3, 5)]
    e = [int(end[i:i + 2], 16) for i in (1, 3, 5)]
    out = []
    for n in range(steps):
        t = n / max(1, steps - 1)
        out.append("#{:02x}{:02x}{:02x}".format(*[round(a + (b - a) * t) for a, b in zip(s, e)]))
    return out


def show_banner():
    lines = render_word("AUTOLYRIC") + [""] + render_word("EMBEDDER")
    if console.width >= max(len(l) for l in lines) + 4:
        colors = gradient("#ff5f87", "#5fd7ff", len(lines))
        art = Text()
        for line, color in zip(lines, colors):
            art.append(line + "\n", style="bold " + color)
        art.rstrip()
    else:
        art = Text("♪ AUTOLYRICEMBEDDER ♪", style="bold magenta")
    console.print()
    console.print(Align.center(art))
    console.print(Align.center(Text(
        "synced lyrics from lrclib.net  →  straight into your audio tags", style="dim italic")))
    console.print()


# --------------------------------------------------------------------------- #
# Small formatting helpers
# --------------------------------------------------------------------------- #
def fmt_duration(seconds):
    if not seconds:
        return "?:??"
    seconds = int(round(seconds))
    return "{}:{:02d}".format(seconds // 60, seconds % 60)


def lyric_badge(rec):
    if rec.get("syncedLyrics"):
        return "[green]● synced[/]"
    if rec.get("instrumental"):
        return "[blue]♪ instrumental[/]"
    if rec.get("plainLyrics"):
        return "[yellow]◐ plain[/]"
    return "[red]✗ none[/]"


def delta_badge(rec, meta):
    if not (rec.get("duration") and meta["duration"]):
        return "[dim]-[/]"
    d = abs(rec["duration"] - meta["duration"])
    color = "green" if d <= 2 else "yellow" if d <= 5 else "red"
    return "[{}]{:.0f}s[/]".format(color, d)


def track_label(meta):
    return "[bold]{}[/] [dim]–[/] {}".format(escape(meta["artist"] or "?"), escape(meta["title"]))


# --------------------------------------------------------------------------- #
# LRCLIB API
# --------------------------------------------------------------------------- #
class LrclibError(Exception):
    """An LRCLIB request failed. str() gives a one-line, user-facing summary."""

    def __init__(self, status, name, message, endpoint):
        Exception.__init__(self, status, name, message, endpoint)
        self.status, self.name, self.message, self.endpoint = status, name, message, endpoint

    def __str__(self):
        head = "HTTP {}".format(self.status) if self.status else "Network error"
        if self.name and self.name != "NetworkError":
            head += " " + self.name
        return "{}{} [/api/{}]".format(head, ": " + self.message if self.message else "",
                                       self.endpoint)


class AuthError(LrclibError):
    """LRCLIB rejected the App ID / API key (HTTP 401). Aborts the whole run."""

    def __str__(self):
        return (LrclibError.__str__(self) +
                " – check your App ID / API key, or run with --reset-client")


def build_user_agent(value):
    """Accept a full User-Agent, or just a contact (email / URL) to append."""
    value = (value or "").strip()
    if not value:
        return APP_NAME
    if " " not in value and ("@" in value or value.startswith(("http://", "https://"))):
        return "{} ({})".format(APP_NAME, value)
    return value


def client_headers():
    headers = {"User-Agent": CLIENT["user_agent"] or APP_NAME}
    if CLIENT["app_id"]:
        headers["Lrclib-App-Id"] = CLIENT["app_id"]
    if CLIENT["api_key"]:
        headers["Lrclib-Api-Key"] = CLIENT["api_key"]
    return headers


def client_description():
    if CLIENT["app_id"]:
        return "[green]registered app[/] [dim](~180 req/min)[/]"
    if CLIENT["user_agent"] != APP_NAME:
        return "[cyan]custom User-Agent[/] [dim](~90 req/min)[/]"
    return "[dim]default User-Agent (~90 req/min)[/]"


def retry_after(err, attempt):
    """Seconds to wait from the Retry-After header, plus up to 30% jitter."""
    try:
        delay = float(err.headers.get("Retry-After"))
    except (TypeError, ValueError, AttributeError):
        delay = 2.0 * (attempt + 1)
    return max(1.0, delay) * (1 + random.uniform(0, 0.3))


def http_error_details(err):
    """(name, message) from an LRCLIB error response. Bodies are usually JSON
    ({"name", "message", "statusCode"}) but may be plain text or HTML."""
    name, message = "", ""
    try:
        body = err.read().decode("utf-8", "replace").strip()
    except Exception:
        body = ""
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            name = str(data.get("name") or "")
            message = str(data.get("message") or "")
    except ValueError:
        if body and not body.lstrip().startswith("<"):  # skip HTML error pages
            message = " ".join(body.split())[:200]
    return name or str(err.reason or ""), message


def warn(message):
    console.print("  [yellow]⚠ {}[/]".format(escape(message)))


def api_request(endpoint, params):
    url = "{}/{}?{}".format(API_BASE, endpoint, urlencode(params))
    req = Request(url, headers=client_headers())
    attempts = 5
    last_error = None
    for attempt in range(attempts):
        # Send requests sequentially with a short gap, as the docs ask.
        wait = MIN_REQUEST_GAP - (time.time() - _last_request[0])
        if wait > 0:
            time.sleep(wait)
        _last_request[0] = time.time()
        try:
            with urlopen(req, timeout=20) as resp:
                raw = resp.read()
                status = resp.status
            try:
                return json.loads(raw.decode("utf-8"))
            except ValueError:
                raise LrclibError(status, "InvalidResponse",
                                  "response was not valid JSON", endpoint)
        except HTTPError as e:
            if e.code == 404:  # TrackNotFound: a normal "no match", not an error
                return None
            name, message = http_error_details(e)
            last_error = LrclibError(e.code, name, message, endpoint)
            if e.code == 401:
                raise AuthError(e.code, name, message, endpoint)
            if e.code == 429 or e.code >= 500:
                if attempt == attempts - 1:
                    break
                delay = retry_after(e, attempt)
                warn("{} – retrying in {:.0f}s (attempt {}/{})".format(
                    last_error, delay, attempt + 2, attempts))
                time.sleep(delay)
                continue
            raise last_error
        except URLError as e:
            last_error = LrclibError(None, "NetworkError", str(e.reason), endpoint)
            if attempt >= 2:
                break
            delay = 2 * (attempt + 1)
            warn("{} – retrying in {}s (attempt {}/3)".format(last_error, delay, attempt + 2))
            time.sleep(delay)
        except (OSError, ValueError) as e:  # timeouts, connection resets, bad chunks
            last_error = LrclibError(None, type(e).__name__, str(e) or "connection failed", endpoint)
            if attempt >= 2:
                break
            delay = 2 * (attempt + 1)
            warn("{} – retrying in {}s (attempt {}/3)".format(last_error, delay, attempt + 2))
            time.sleep(delay)
    raise LrclibError(last_error.status, last_error.name,
                      (last_error.message + " – " if last_error.message else "")
                      + "gave up after retrying", endpoint)


def load_client_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return {k: str(data.get(k) or "") for k in ("user_agent", "app_id", "api_key")}
    except (OSError, ValueError):
        return {}


def save_client_config():
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in CLIENT.items() if v and k != "api_key" and not (k == "user_agent" and v == APP_NAME)},
                  f, indent=2)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


def clean_title(title):
    """Strip things like '(feat. X)', '[Remastered 2011]', '- Live' that often
    prevent a match."""
    t = re.sub(r"\s*[\(\[][^\)\]]*(feat|ft\.|remaster|live|version|edit|mix|"
               r"mono|stereo|bonus|deluxe)[^\)\]]*[\)\]]", "", title, flags=re.I)
    t = re.sub(r"\s+-\s+.*(remaster|live|version|edit|mix|mono|stereo).*$", "",
               t, flags=re.I)
    return t.strip() or title


def score_result(result, meta):
    """Lower is better. Prefers synced lyrics and close durations."""
    score = 0.0
    if meta["duration"] and result.get("duration"):
        score += abs(result["duration"] - meta["duration"])
    else:
        score += 30
    if not result.get("syncedLyrics"):
        score += 1000 if not result.get("instrumental") else 500
    if meta["album"] and (result.get("albumName") or "").lower() != meta["album"].lower():
        score += 1
    return score


def find_lyrics(meta):
    """Return a list of candidate LRCLIB records, best first."""
    candidates = []

    # 1) Exact signature lookup (needs all four fields).
    if meta["title"] and meta["artist"] and meta["album"] and meta["duration"]:
        rec = api_request("get", {
            "track_name": meta["title"],
            "artist_name": meta["artist"],
            "album_name": meta["album"],
            "duration": int(round(meta["duration"])),
        })
        if rec:
            candidates.append(rec)

    # 2) Search by title + artist (raw, then cleaned title).
    if not candidates or not candidates[0].get("syncedLyrics"):
        titles = [meta["title"]]
        if clean_title(meta["title"]) != meta["title"]:
            titles.append(clean_title(meta["title"]))
        for title in titles:
            params = {"track_name": title}
            if meta["artist"]:
                params["artist_name"] = meta["artist"]
            results = api_request("search", params) or []
            if results:
                candidates.extend(results)
                break

    # De-duplicate by id and rank.
    seen, unique = set(), []
    for c in candidates:
        if c.get("id") not in seen:
            seen.add(c.get("id"))
            unique.append(c)

    # Reject results whose duration is way off (probably a different song).
    if meta["duration"]:
        unique = [c for c in unique
                  if not c.get("duration") or abs(c["duration"] - meta["duration"]) <= 10]

    unique.sort(key=lambda c: score_result(c, meta))
    return unique


def manual_search(query):
    return api_request("search", {"q": query}) or []


# --------------------------------------------------------------------------- #
# Reading tags
# --------------------------------------------------------------------------- #
def first(value):
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return str(value).strip() if value is not None else ""


def read_metadata(path):
    audio = mutagen.File(path, easy=True)
    if audio is None:
        return None
    tags = audio.tags or {}

    def get(key):
        try:
            return first(tags.get(key))
        except Exception:
            return ""

    meta = {
        "title": get("title"),
        "artist": get("artist") or get("albumartist"),
        "album": get("album"),
        "duration": getattr(audio.info, "length", 0) or 0,
    }

    # Fall back to "Artist - Title.ext" style filenames.
    if not meta["title"]:
        stem = os.path.splitext(os.path.basename(path))[0]
        stem = re.sub(r"^\d+[\s.\-_]+", "", stem)  # drop leading track numbers
        if " - " in stem and not meta["artist"]:
            meta["artist"], meta["title"] = [s.strip() for s in stem.split(" - ", 1)]
        else:
            meta["title"] = stem.strip()
    return meta


# --------------------------------------------------------------------------- #
# Writing tags
# --------------------------------------------------------------------------- #
def lrc_to_sylt(lrc):
    """Convert LRC text into ID3 SYLT [(text, milliseconds), ...]."""
    entries = []
    for line in lrc.splitlines():
        stamps = LRC_LINE.findall(line)
        if not stamps:
            continue
        text = LRC_LINE.sub("", line).strip()
        for mins, secs in stamps:
            entries.append((text, int((int(mins) * 60 + float(secs)) * 1000)))
    entries.sort(key=lambda e: e[1])
    return entries


def has_lyrics(path):
    audio = mutagen.File(path)
    if audio is None or audio.tags is None:
        return False
    tags = audio.tags
    if isinstance(tags, ID3):
        return bool(tags.getall("USLT") or tags.getall("SYLT"))
    if isinstance(audio, MP4):
        return "\xa9lyr" in tags
    if isinstance(audio, ASF):
        return "WM/Lyrics" in tags
    try:
        return "lyrics" in tags or "unsyncedlyrics" in tags
    except Exception:
        return False


class MetadataError(Exception):
    """Writing lyrics would have changed other metadata; the file was left untouched."""


def _is_lyrics_key(key):
    key = str(key)
    return (key.split(":")[0] in ("USLT", "SYLT") or
            key.lower() in ("\xa9lyr", "wm/lyrics", "lyrics", "unsyncedlyrics"))


def _open_tags(path):
    """Returns (audio, tags, had_tags). MP3 ID3 tags are loaded with
    translate=False so ID3v2.3 frames aren't converted or dropped in memory."""
    audio = mutagen.File(path)
    if audio is None:
        raise ValueError("unsupported file")
    if isinstance(audio, MP3):
        try:
            return audio, ID3(path, translate=False), True
        except ID3NoHeaderError:
            return audio, ID3(), False
    if audio.tags is None:
        audio.add_tags()
        return audio, audio.tags, False
    return audio, audio.tags, True


def _digest(value):
    return hashlib.sha1(repr(value).encode("utf-8", "surrogatepass")).hexdigest()


def tag_snapshot(path):
    """Fingerprint of every non-lyrics tag and embedded picture in a file."""
    audio, tags, had = _open_tags(path)
    snap = {}
    if not had:
        return snap, None
    version = None
    if isinstance(tags, ID3):
        version = tags.version
        for frame in tags.values():
            if not _is_lyrics_key(frame.FrameID):
                snap[frame.HashKey] = _digest(frame)
        for i, raw in enumerate(getattr(tags, "unknown_frames", None) or []):
            snap["unknown#{}".format(i)] = hashlib.sha1(raw).hexdigest()
    else:
        grouped = {}
        for key, value in tags.items():
            if not _is_lyrics_key(key):
                grouped.setdefault(str(key).lower(), []).append(_digest(value))
        snap.update({k: sorted(v) for k, v in grouped.items()})
    for i, pic in enumerate(getattr(audio, "pictures", None) or []):  # FLAC picture blocks
        snap["picture#{}".format(i)] = hashlib.sha1(pic.data).hexdigest() + str(pic.type)
    return snap, version


def _apply_lyrics(path, lyrics, synced, write_sylt):
    audio, tags, had = _open_tags(path)

    if isinstance(tags, ID3):  # MP3, WAV, AIFF
        tags.delall("USLT")
        tags.add(USLT(encoding=3, lang="XXX", desc="", text=lyrics))
        tags.delall("SYLT")
        if synced and write_sylt:
            entries = lrc_to_sylt(lyrics)
            if entries:
                tags.add(SYLT(encoding=3, lang="XXX", format=2, type=1,
                              desc="", text=entries))
        # Keep the file's ID3 version: upgrading v2.3 -> v2.4 drops unknown and
        # v2.3-only frames, and some players/car stereos can't read v2.4.
        # New tags (and v1/v2.2, which can't be written back) use v2.3 for compatibility.
        version = 4 if had and tags.version >= (2, 4, 0) else 3
        if isinstance(audio, MP3):
            tags.save(path, v2_version=version)  # v1=1 default: keep an existing ID3v1 tag
        else:
            audio.save(v2_version=version)
        return
    if isinstance(audio, MP4):
        tags["\xa9lyr"] = [lyrics]
    elif isinstance(audio, ASF):
        tags["WM/Lyrics"] = lyrics
    else:  # Vorbis comments: FLAC, OGG, Opus
        for key in ("UNSYNCEDLYRICS", "unsyncedlyrics"):
            if key in tags:
                del tags[key]
        tags["LYRICS"] = lyrics
    audio.save()


def write_lyrics(path, lyrics, synced, write_sylt=True):
    """Embed lyrics without touching any other metadata.

    The tags are written to a temporary copy first. The copy only replaces the
    original if every other tag and picture is byte-for-byte unchanged, so a
    failure or a lossy save can never damage the original file."""
    before, version = tag_snapshot(path)
    folder, name = os.path.split(path)
    stem, ext = os.path.splitext(name)
    tmp = os.path.join(folder, ".{}.lyrics-tmp{}".format(stem, ext))
    try:
        shutil.copy2(path, tmp)
        _apply_lyrics(tmp, lyrics, synced, write_sylt)
        after, new_version = tag_snapshot(tmp)
        # ID3v1/v2.2 sources are necessarily converted; for those only pictures must match.
        strict = version is None or version >= (2, 3, 0)
        keys = before if strict else [k for k in before if k.startswith(("APIC", "picture#"))]
        lost = sorted(k for k in keys if k not in after)
        changed = sorted(k for k in keys if k in after and after[k] != before[k])
        if strict and version is not None and new_version is not None and \
                version[:2] != new_version[:2]:
            changed.append("ID3 version {}.{} -> {}.{}".format(*(version[:2] + new_version[:2])))
        if lost or changed:
            details = ", ".join(["lost " + k for k in lost] + ["changed " + k for k in changed])
            raise MetadataError("aborted, file left untouched – writing would alter other "
                                "metadata ({})".format(details[:300]))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def write_lrc_file(path, lyrics):
    lrc_path = os.path.splitext(path)[0] + ".lrc"
    with open(lrc_path, "w", encoding="utf-8") as f:
        f.write(lyrics)
    return lrc_path


# --------------------------------------------------------------------------- #
# Scanning
# --------------------------------------------------------------------------- #
def _windows_known_music_folder():
    """Ask Windows where the Music folder is (follows OneDrive/other redirection)."""
    try:
        import ctypes
        import uuid
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        folder_id = GUID.from_buffer_copy(uuid.UUID("4BD8D571-6D19-48D3-BE97-422220080E43").bytes_le)
        ptr = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None,
                                                      ctypes.byref(ptr)) != 0:
            return None
        path = ptr.value
        ctypes.windll.ole32.CoTaskMemFree(ptr)
        return path
    except Exception:
        return None


def _xdg_music_folder():
    """Linux: XDG_MUSIC_DIR from ~/.config/user-dirs.dirs (may be localized)."""
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    try:
        with open(os.path.join(config_home, "user-dirs.dirs"), encoding="utf-8") as f:
            for line in f:
                m = re.match(r'\s*XDG_MUSIC_DIR\s*=\s*"?([^"\n]*)"?', line)
                if m:
                    path = m.group(1).replace("$HOME", os.path.expanduser("~"))
                    return os.path.expandvars(path)
    except OSError:
        pass
    return None


def music_folders():
    """Default music folders that exist on this system, as [(path, label)], best first."""
    home = os.path.expanduser("~")
    candidates = []
    if os.name == "nt":
        candidates.append((_windows_known_music_folder(), "Music library"))
        profile = os.environ.get("USERPROFILE") or home
        candidates.append((os.path.join(profile, "Music"), "Music"))
        for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
            if os.environ.get(var):
                candidates.append((os.path.join(os.environ[var], "Music"), "OneDrive"))
        candidates.append((os.path.join(profile, "OneDrive", "Music"), "OneDrive"))
    elif sys.platform == "darwin":
        candidates.append((os.path.join(home, "Music"), "Music"))
    else:
        candidates.append((_xdg_music_folder(), "XDG music folder"))
        candidates.append((os.path.join(home, "Music"), "Music"))

    seen, found = set(), []
    for path, label in candidates:
        if not path or not os.path.isdir(path):
            continue
        key = os.path.normcase(os.path.realpath(path))
        if key not in seen:
            seen.add(key)
            found.append((os.path.abspath(path), label))
    return found


def pretty_path(path):
    home = os.path.expanduser("~")
    if os.path.normcase(path).startswith(os.path.normcase(home + os.sep)):
        return "~" + path[len(home):]
    return path


def collect_files(directory, recursive):
    files = []
    if recursive:
        for root, _, names in os.walk(directory):
            files.extend(os.path.join(root, n) for n in names)
    else:
        files = [os.path.join(directory, n) for n in os.listdir(directory)]
    files = [f for f in files
             if os.path.isfile(f) and os.path.splitext(f)[1].lower() in AUDIO_EXTS
             and ".lyrics-tmp" not in os.path.basename(f)]  # leftovers of an interrupted write
    return sorted(files, key=lambda p: p.lower())


def scan(files):
    """Read tags for every file up front. Returns a list of dicts."""
    items = []
    with console.status("[cyan]Reading tags…", spinner="dots"):
        for path in files:
            item = {"path": path, "meta": None, "has_lyrics": False, "error": None}
            try:
                item["meta"] = read_metadata(path)
                if item["meta"] is None:
                    item["error"] = "unsupported or unreadable"
                else:
                    item["has_lyrics"] = has_lyrics(path)
            except Exception as e:
                item["error"] = str(e)
            items.append(item)
    return items


def show_library(items, directory, limit=25):
    console.print("[bold]{}[/] audio file(s) in [cyan]{}[/]".format(len(items), escape(directory)))
    table = Table(box=box.SIMPLE_HEAVY, header_style="bold cyan", expand=False)
    table.add_column("#", justify="right", style="dim")
    table.add_column("Artist", overflow="ellipsis", max_width=24)
    table.add_column("Title", overflow="ellipsis", max_width=34)
    table.add_column("Album", overflow="ellipsis", max_width=24, style="dim")
    table.add_column("Length", justify="right")
    table.add_column("Lyrics", justify="center")
    for n, item in enumerate(items[:limit], 1):
        meta = item["meta"]
        if item["error"]:
            table.add_row(str(n), "[red]error[/]",
                          escape(os.path.basename(item["path"])), "", "", "[red]✗[/]")
            continue
        table.add_row(str(n), escape(meta["artist"] or "?"), escape(meta["title"]),
                      escape(meta["album"] or "-"), fmt_duration(meta["duration"]),
                      "[green]✔[/]" if item["has_lyrics"] else "[dim]·[/]")
    if len(items) > limit:
        table.add_row("", "[dim]… and {} more[/]".format(len(items) - limit), "", "", "", "")
    console.print(table)


# --------------------------------------------------------------------------- #
# Interactive pieces
# --------------------------------------------------------------------------- #
def matches_table(candidates, meta, selected):
    table = Table(box=box.ROUNDED, header_style="bold magenta", show_lines=False)
    table.add_column("", width=1)
    table.add_column("#", justify="right")
    table.add_column("Artist", overflow="ellipsis", max_width=22)
    table.add_column("Title", overflow="ellipsis", max_width=30)
    table.add_column("Album", overflow="ellipsis", max_width=24, style="dim")
    table.add_column("Length", justify="right")
    table.add_column("Δ", justify="right")
    table.add_column("Lyrics")
    for i, c in enumerate(candidates[:MAX_CHOICES]):
        sel = i == selected
        table.add_row("[bold cyan]▶[/]" if sel else "", str(i + 1),
                      escape(c.get("artistName") or "?"), escape(c.get("trackName") or "?"),
                      escape(c.get("albumName") or "-"), fmt_duration(c.get("duration")),
                      delta_badge(c, meta), lyric_badge(c),
                      style="bold" if sel else None)
    return table


def show_preview(rec, lines=12):
    text = rec.get("syncedLyrics") or rec.get("plainLyrics") or ""
    body = [l for l in text.splitlines() if l.strip()]
    out = Text()
    for line in body[:lines]:
        m = re.match(r"^((?:\[\d+:\d+(?:\.\d+)?\])+)\s*(.*)$", line)
        if m:
            out.append(m.group(1) + " ", style="cyan dim")
            out.append(m.group(2) + "\n")
        else:
            out.append(line + "\n")
    if len(body) > lines:
        out.append("… {} more lines".format(len(body) - lines), style="dim italic")
    out.rstrip()
    console.print(Panel(out if body else Text("(empty)", style="dim"),
                        title="[bold]{} – {}[/]".format(escape(rec.get("artistName") or "?"),
                                                        escape(rec.get("trackName") or "?")),
                        title_align="left", border_style="cyan", box=box.ROUNDED,
                        padding=(0, 2), expand=False))


KEYS_HELP = ("[bold cyan]Enter[/] use ▶   [bold cyan]1-{}[/] select   "
             "[bold cyan]p[/] preview   [bold cyan]s[/] search   [bold cyan]n[/] skip   "
             "[bold cyan]a[/] auto rest   [bold cyan]q[/] quit").format(MAX_CHOICES)


def choose_interactively(candidates, meta):
    """Let the user pick a candidate. Returns a record, None (skip), 'auto' or 'quit'."""
    selected = 0
    redraw = True
    while True:
        if redraw:
            if candidates:
                console.print(matches_table(candidates, meta, selected))
            else:
                console.print("  [yellow]No matches found on LRCLIB.[/] "
                              "Press [bold cyan]s[/] to search with your own query.")
            console.print("  " + KEYS_HELP, style="dim")
        redraw = False
        try:
            choice = console.input("  [bold magenta]›[/] ").strip().lower()
        except EOFError:
            return "quit"

        if choice == "" and candidates:
            return candidates[selected]
        if choice.isdigit() and 1 <= int(choice) <= min(MAX_CHOICES, len(candidates)):
            selected = int(choice) - 1
            show_preview(candidates[selected])
            redraw = True
        elif choice == "p" and candidates:
            show_preview(candidates[selected])
        elif choice == "s":
            default_q = "{} {}".format(meta["artist"], meta["title"]).strip()
            query = Prompt.ask("  [bold]Search LRCLIB for[/]", default=default_q, console=console)
            try:
                with console.status("[cyan]Searching…", spinner="dots"):
                    candidates = manual_search(query)
            except AuthError:
                raise
            except Exception as e:
                console.print("  [red]Search failed:[/] {}".format(escape(str(e))))
            candidates.sort(key=lambda c: score_result(c, meta))
            selected = 0
            redraw = True
        elif choice == "n":
            return None
        elif choice == "a":
            return "auto"
        elif choice == "q":
            return "quit"
        else:
            console.print("  [dim]Unknown key – try one of the options above.[/]")


def choose_folder(script_dir):
    """Menu of default music folders + the script's folder + 'other'."""
    options = list(music_folders())
    if not any(os.path.normcase(p) == os.path.normcase(script_dir) for p, _ in options):
        options.append((script_dir, "this script's folder"))

    table = Table.grid(padding=(0, 2))
    table.add_column(justify="right", style="bold cyan")
    table.add_column()
    table.add_column(style="dim")
    for n, (path, label) in enumerate(options, 1):
        table.add_row(str(n), escape(pretty_path(path)), label)
    table.add_row(str(len(options) + 1), "Other folder…", "type a path")
    console.print("[bold]Where are your music files?[/]")
    console.print(table)

    choices = [str(n) for n in range(1, len(options) + 2)]
    pick = int(Prompt.ask("[bold]Folder[/]", choices=choices, default="1",
                          show_choices=False, console=console))
    if pick <= len(options):
        return options[pick - 1][0]
    while True:
        directory = Prompt.ask("[bold]Folder path[/]", console=console)
        directory = os.path.abspath(os.path.expanduser(directory.strip().strip('"')))
        if os.path.isdir(directory):
            return directory
        console.print("  [red]✖ Not a folder:[/] {}".format(escape(directory)))


def setup(args, script_dir):
    """Interactive first-run questions. Returns the target directory."""
    console.print(Panel(
        "Answer a few questions, or just press [bold cyan]Enter[/] to accept the defaults.\n"
        "[dim]Tip: run with [bold]--help[/bold] to see command-line options.[/]",
        title="[bold]Setup[/]", title_align="left", border_style="magenta",
        box=box.ROUNDED, expand=False))
    directory = choose_folder(script_dir)
    # Music libraries are usually Artist/Album/… so default to scanning subfolders there.
    is_library = any(os.path.normcase(directory) == os.path.normcase(p) for p, _ in music_folders())
    args.recursive = Confirm.ask("Include subfolders?", default=args.recursive or is_library,
                                 console=console)
    args.overwrite = Confirm.ask("Overwrite lyrics that are already embedded?",
                                 default=args.overwrite, console=console)
    args.plain = Confirm.ask("Use plain (untimed) lyrics if no synced version exists?",
                             default=True, console=console)
    args.lrc = Confirm.ask("Also save .lrc files next to the audio?",
                           default=args.lrc, console=console)
    args.auto = not Confirm.ask("Review each match before writing?", default=True, console=console)
    setup_client()
    return directory


def setup_client():
    """Optional: identify this client to LRCLIB for higher rate limits."""
    configured = bool(CLIENT["app_id"] or CLIENT["user_agent"] != APP_NAME)
    question = ("Change your saved LRCLIB identification?" if configured else
                "Set up LRCLIB identification for higher rate limits? [dim](optional)[/]")
    if configured:
        console.print("  [dim]LRCLIB client:[/] " + client_description())
    if not Confirm.ask(question, default=False, console=console):
        return
    console.print(Panel(
        "LRCLIB limits requests per client:\n"
        "  [dim]•[/] generic / anonymous client      [dim]~45 req/min[/]\n"
        "  [dim]•[/] [cyan]User-Agent[/] naming the app      [dim]~90 req/min[/]\n"
        "  [dim]•[/] registered [cyan]App ID[/]              [dim]~180 req/min[/]\n\n"
        "Get a free App ID (client-side app) at [link=https://lrclib.net/developers]"
        "[bold]https://lrclib.net/developers[/][/link].\n"
        "[dim]For the User-Agent you can type a full string, or just an email / URL "
        "to add as contact info.[/]",
        title="[bold]LRCLIB client identification[/]", title_align="left",
        border_style="cyan", box=box.ROUNDED, expand=False))
    CLIENT["user_agent"] = build_user_agent(Prompt.ask(
        "[bold]User-Agent or contact[/]", default=CLIENT["user_agent"], console=console))
    app_id = Prompt.ask("[bold]App ID[/] [dim](blank for none)[/]",
                        default=CLIENT["app_id"] or "", show_default=bool(CLIENT["app_id"]),
                        console=console).strip()
    CLIENT["app_id"] = "" if app_id.lower() in ("-", "none") else app_id
    console.print("  [dim]Using:[/] {}  [dim]→[/] {}".format(
        escape(CLIENT["user_agent"]), client_description()))
    if Confirm.ask("Save this for next time?", default=True, console=console):
        try:
            save_client_config()
            console.print("  [green]✔ Saved to[/] {}".format(escape(CONFIG_PATH)))
        except OSError as e:
            console.print("  [red]✖ Could not save:[/] {}".format(escape(str(e))))


def show_settings(args):
    def flag(v):
        return "[green]✔ yes[/]" if v else "[dim]✗ no[/]"
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column()
    grid.add_row("Mode", "[cyan]automatic[/]" if args.auto else "[magenta]review each match[/]")
    grid.add_row("Subfolders", flag(args.recursive))
    grid.add_row("Overwrite existing", flag(args.overwrite))
    grid.add_row("Plain-lyrics fallback", flag(args.plain))
    grid.add_row(".lrc sidecar files", flag(args.lrc))
    grid.add_row("ID3 SYLT frame", flag(not args.no_sylt))
    grid.add_row("LRCLIB client", client_description())
    if args.dry_run:
        grid.add_row("Dry run", "[yellow]✔ nothing will be written[/]")
    console.print(Panel(grid, title="[bold]Settings[/]", title_align="left",
                        border_style="blue", box=box.ROUNDED, expand=False))


# --------------------------------------------------------------------------- #
# Processing
# --------------------------------------------------------------------------- #
def apply_record(item, rec, args):
    """Write rec's lyrics into the file. Returns (status, message)."""
    if rec.get("instrumental"):
        return "skipped", "[blue]♪ instrumental on LRCLIB – skipped[/]"
    lyrics, synced = rec.get("syncedLyrics"), True
    if not lyrics:
        if args.plain and rec.get("plainLyrics"):
            lyrics, synced = rec["plainLyrics"], False
        else:
            return "not_found", "[yellow]◐ only plain lyrics available – skipped[/]"
    kind = "synced" if synced else "plain"
    if args.dry_run:
        return kind, "[yellow]○ would embed {} lyrics (dry run)[/]".format(kind)
    try:
        write_lyrics(item["path"], lyrics, synced, write_sylt=not args.no_sylt)
        if args.lrc:
            write_lrc_file(item["path"], lyrics)
    except Exception as e:
        return "errors", "[red]✖ write failed: {}[/]".format(escape(str(e)))
    return kind, "[green]✔ embedded {} lyrics{}[/]".format(kind, " + .lrc" if args.lrc else "")


def precheck(item, args):
    """Returns (status, message) if the file should not be looked up, else None."""
    if item["error"]:
        return "errors", "[red]✖ {}[/]".format(escape(item["error"]))
    if item["has_lyrics"] and not args.overwrite:
        return "skipped", "[dim]⏭ already has lyrics[/]"
    return None


def process_interactive(item, n, total, args):
    """Returns (status, message, control) where control is None/'auto'/'quit'."""
    meta = item["meta"]
    console.print()
    console.rule("[bold magenta]Track {}/{}[/]".format(n, total), style="magenta", align="left")
    console.print("  [dim]File[/]  {}".format(escape(os.path.basename(item["path"]))))
    if meta:
        console.print("  [dim]Tags[/]  {}  [dim]· {} · {}[/]".format(
            track_label(meta), escape(meta["album"] or "no album"), fmt_duration(meta["duration"])))
    early = precheck(item, args)
    if early:
        return early + (None,)

    try:
        with console.status("[cyan]Searching LRCLIB…", spinner="dots"):
            candidates = find_lyrics(meta)
    except AuthError:
        raise
    except Exception as e:
        return "errors", "[red]✖ LRCLIB request failed: {}[/]".format(escape(str(e))), None

    rec = choose_interactively(candidates, meta)
    if rec == "quit":
        return None, None, "quit"
    control = None
    if rec == "auto":
        control = "auto"
        rec = candidates[0] if candidates else None
    if not rec:
        return ("not_found" if not candidates else "skipped"), "[dim]⏭ no lyrics applied[/]", control
    return apply_record(item, rec, args) + (control,)


def process_auto(item, args):
    early = precheck(item, args)
    if early:
        return early
    try:
        candidates = find_lyrics(item["meta"])
    except AuthError:
        raise
    except Exception as e:
        return "errors", "[red]✖ LRCLIB request failed: {}[/]".format(escape(str(e)))
    if not candidates:
        return "not_found", "[yellow]✗ not found on LRCLIB[/]"
    return apply_record(item, candidates[0], args)


def run_auto(items, start, args, stats):
    total = len(items)
    with Progress(SpinnerColumn("dots", style="magenta"),
                  TextColumn("[progress.description]{task.description}"),
                  BarColumn(bar_width=None, complete_style="magenta", finished_style="green"),
                  MofNCompleteColumn(), TimeElapsedColumn(),
                  console=console, transient=True) as progress:
        task = progress.add_task("", total=total - start)
        for n in range(start, total):
            item = items[n]
            name = item["meta"]["title"] if item["meta"] else os.path.basename(item["path"])
            progress.update(task, description="[cyan]{}[/]".format(escape(name[:40])))
            status, message = process_auto(item, args)
            stats[status] += 1
            label = track_label(item["meta"]) if item["meta"] else escape(os.path.basename(item["path"]))
            progress.console.print("  [dim]{:>{w}}/{}[/]  {}  {}".format(
                n + 1, total, label, message, w=len(str(total))))
            progress.advance(task)


def show_summary(stats, elapsed):
    table = Table.grid(padding=(0, 3))
    table.add_column(style="bold")
    table.add_column(justify="right")
    table.add_row("[green]● Synced lyrics[/]", str(stats["synced"]))
    table.add_row("[yellow]◐ Plain lyrics[/]", str(stats["plain"]))
    table.add_row("[dim]⏭ Skipped[/]", str(stats["skipped"]))
    table.add_row("[yellow]✗ Not found[/]", str(stats["not_found"]))
    table.add_row("[red]✖ Errors[/]", str(stats["errors"]))
    console.print()
    console.print(Panel(table, title="[bold]Done[/] [dim]in {:.1f}s[/]".format(elapsed),
                        title_align="left", border_style="green", box=box.ROUNDED,
                        padding=(0, 2), expand=False))


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(
        description="Fetch timestamped lyrics from LRCLIB and embed them in audio files.")
    parser.add_argument("directory", nargs="?", help="folder with audio files "
                        "(default: ask, offering your Music folder)")
    parser.add_argument("-m", "--music", action="store_true",
                        help="use your default Music folder (Windows Music library / OneDrive, "
                             "macOS ~/Music, Linux XDG music dir); combine with -r")
    parser.add_argument("--list-music", action="store_true",
                        help="print the detected Music folders and exit")
    parser.add_argument("-r", "--recursive", action="store_true", help="include subfolders")
    parser.add_argument("-o", "--overwrite", action="store_true",
                        help="replace lyrics that are already embedded")
    parser.add_argument("-a", "--auto", action="store_true",
                        help="don't ask per file; use the best match automatically")
    parser.add_argument("--lrc", action="store_true", help="also write .lrc sidecar files")
    parser.add_argument("--plain", action="store_true",
                        help="embed plain lyrics when no synced lyrics exist")
    parser.add_argument("--no-sylt", action="store_true",
                        help="MP3/WAV/AIFF: don't write the ID3 SYLT frame")
    parser.add_argument("--dry-run", action="store_true", help="show what would happen, write nothing")
    ident = parser.add_argument_group(
        "LRCLIB client identification (optional, raises rate limits; "
        "also read from LRCLIB_USER_AGENT / LRCLIB_APP_ID / LRCLIB_API_KEY)")
    ident.add_argument("--user-agent", metavar="UA",
                       help="custom User-Agent, or just an email/URL to add as contact")
    ident.add_argument("--app-id", help="registered Lrclib-App-Id from lrclib.net/developers")
    ident.add_argument("--api-key", help="Lrclib-Api-Key (server-side apps only; never saved)")
    ident.add_argument("--save-client", action="store_true",
                       help="save --user-agent/--app-id to " + CONFIG_PATH)
    ident.add_argument("--reset-client", action="store_true",
                       help="forget saved identification and use the defaults")
    args = parser.parse_args()

    # Precedence: command line > environment > saved config > default.
    if args.reset_client and os.path.exists(CONFIG_PATH):
        os.remove(CONFIG_PATH)
    saved = {} if args.reset_client else load_client_config()
    env = os.environ.get
    CLIENT["user_agent"] = build_user_agent(
        args.user_agent or env("LRCLIB_USER_AGENT") or saved.get("user_agent"))
    CLIENT["app_id"] = (args.app_id or env("LRCLIB_APP_ID") or saved.get("app_id") or "").strip()
    CLIENT["api_key"] = (args.api_key or env("LRCLIB_API_KEY") or "").strip()
    if args.save_client:
        save_client_config()

    if args.list_music:
        for path, label in music_folders() or [("(none found)", "")]:
            console.print("{}  [dim]{}[/]".format(escape(path), label))
        return 0
    if args.music and args.directory:
        parser.error("use either a directory or --music, not both")

    show_banner()

    interactive = args.directory is None and not args.music
    if interactive:
        directory = setup(args, script_dir)
    elif args.music:
        folders = music_folders()
        if not folders:
            console.print("[red]✖ Couldn't find a default Music folder.[/] Pass a directory instead.")
            return 1
        directory = folders[0][0]
        console.print("[bold]Music folder:[/] {}  [dim]({})[/]".format(escape(directory), folders[0][1]))
        if len(folders) > 1:
            console.print("[dim]Also found: {} – pass one as the directory to use it instead.[/]".format(
                ", ".join(escape(p) for p, _ in folders[1:])))
    else:
        directory = os.path.abspath(os.path.expanduser(args.directory.strip('"')))
        if not os.path.isdir(directory):
            console.print("[red]✖ Not a folder:[/] {}".format(escape(directory)))
            return 1

    console.print()
    show_settings(args)

    files = collect_files(directory, args.recursive)
    if not files:
        console.print("[yellow]No supported audio files found in[/] {}".format(escape(directory)))
        if not args.recursive and any(os.path.isdir(os.path.join(directory, d))
                                      for d in os.listdir(directory)):
            console.print("[dim]Tip: this folder has subfolders – include them with [bold]-r[/bold].[/]")
        return 0
    items = scan(files)
    show_library(items, directory)

    todo = sum(1 for i in items if not i["error"] and (args.overwrite or not i["has_lyrics"]))
    if todo == 0:
        console.print("[green]Everything already has lyrics.[/] "
                      "Use overwrite ([bold]-o[/]) to fetch them again.")
        return 0
    if interactive and not Confirm.ask(
            "Look up lyrics for [bold]{}[/] file(s)?".format(todo), default=True, console=console):
        return 0

    stats = {"synced": 0, "plain": 0, "skipped": 0, "not_found": 0, "errors": 0}
    started = time.time()
    n = 0
    code = 0
    try:
        if not args.auto:
            while n < len(items):
                status, message, control = process_interactive(items[n], n + 1, len(items), args)
                if control == "quit":
                    console.print("  [dim]Stopped by user.[/]")
                    break
                stats[status] += 1
                console.print("  " + message)
                n += 1
                if control == "auto":
                    console.print("  [cyan]Switching to automatic mode for the remaining files…[/]\n")
                    args.auto = True
                    break
        if args.auto and n < len(items):
            run_auto(items, n, args, stats)
    except AuthError as e:
        console.print("\n[red]✖ {}[/]".format(escape(str(e))))
        code = 1

    show_summary(stats, time.time() - started)
    return code


if __name__ == "__main__":
    try:
        code = main()
    except KeyboardInterrupt:
        console.print("\n[dim]Cancelled.[/]")
        code = 130
    if len(sys.argv) == 1 and os.name == "nt":
        try:
            console.input("\n[dim]Press Enter to exit…[/]")  # keep window open when double-clicked
        except (EOFError, KeyboardInterrupt):
            pass
    sys.exit(code)
