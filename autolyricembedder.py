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

Requires:  pip install mutagen
Usage:     python autolyricembedder.py [directory] [options]
           (run with no arguments for fully interactive mode)
"""

from __future__ import print_function

import argparse
import json
import os
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    import mutagen
    from mutagen.asf import ASF
    from mutagen.id3 import ID3, SYLT, USLT
    from mutagen.mp4 import MP4
except ImportError:
    print("This script needs the 'mutagen' package. Install it with:\n"
          "    pip install mutagen")
    sys.exit(1)

# Make sure non-ASCII titles/lyrics print on Windows consoles.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

API_BASE = "https://lrclib.net/api"
USER_AGENT = "AutoLyricEmbedder/1.0 (+https://lrclib.net)"
AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".mp4", ".m4b", ".alac", ".ogg",
              ".oga", ".opus", ".wav", ".aif", ".aiff", ".wma"}
LRC_LINE = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")


# --------------------------------------------------------------------------- #
# Console helpers
# --------------------------------------------------------------------------- #
def ask(prompt, default=None):
    suffix = " [{}]".format(default) if default not in (None, "") else ""
    try:
        answer = input("{}{}: ".format(prompt, suffix)).strip()
    except EOFError:
        answer = ""
    return answer if answer else (default or "")


def ask_yes_no(prompt, default=True):
    hint = "Y/n" if default else "y/N"
    while True:
        answer = ask("{} ({})".format(prompt, hint)).lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("  Please answer y or n.")


def fmt_duration(seconds):
    if not seconds:
        return "?:??"
    seconds = int(round(seconds))
    return "{}:{:02d}".format(seconds // 60, seconds % 60)


# --------------------------------------------------------------------------- #
# LRCLIB API
# --------------------------------------------------------------------------- #
def api_request(endpoint, params):
    url = "{}/{}?{}".format(API_BASE, endpoint, urlencode(params))
    req = Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(3):
        try:
            with urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            if e.code == 404:
                return None
            if e.code == 429 or e.code >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            raise
        except URLError:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    return None


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


def write_lyrics(path, lyrics, synced, write_sylt=True):
    audio = mutagen.File(path)
    if audio is None:
        raise ValueError("unsupported file")
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags

    if isinstance(tags, ID3):  # MP3, WAV, AIFF
        tags.delall("USLT")
        tags.add(USLT(encoding=3, lang="XXX", desc="", text=lyrics))
        tags.delall("SYLT")
        if synced and write_sylt:
            entries = lrc_to_sylt(lyrics)
            if entries:
                tags.add(SYLT(encoding=3, lang="XXX", format=2, type=1,
                              desc="", text=entries))
    elif isinstance(audio, MP4):
        tags["\xa9lyr"] = [lyrics]
    elif isinstance(audio, ASF):
        tags["WM/Lyrics"] = lyrics
    else:  # Vorbis comments: FLAC, OGG, Opus
        for key in ("UNSYNCEDLYRICS", "unsyncedlyrics"):
            if key in tags:
                del tags[key]
        tags["LYRICS"] = lyrics
    audio.save()


def write_lrc_file(path, lyrics):
    lrc_path = os.path.splitext(path)[0] + ".lrc"
    with open(lrc_path, "w", encoding="utf-8") as f:
        f.write(lyrics)
    return lrc_path


# --------------------------------------------------------------------------- #
# Main flow
# --------------------------------------------------------------------------- #
def collect_files(directory, recursive):
    files = []
    if recursive:
        for root, _, names in os.walk(directory):
            files.extend(os.path.join(root, n) for n in names)
    else:
        files = [os.path.join(directory, n) for n in os.listdir(directory)]
    files = [f for f in files
             if os.path.isfile(f) and os.path.splitext(f)[1].lower() in AUDIO_EXTS]
    return sorted(files, key=lambda p: p.lower())


def describe(rec):
    kind = ("synced" if rec.get("syncedLyrics")
            else "instrumental" if rec.get("instrumental")
            else "plain only" if rec.get("plainLyrics") else "no lyrics")
    return "{} - {} [{}] ({}) <{}>".format(
        rec.get("artistName", "?"), rec.get("trackName", "?"),
        rec.get("albumName") or "-", fmt_duration(rec.get("duration")), kind)


def preview(rec, lines=8):
    text = rec.get("syncedLyrics") or rec.get("plainLyrics") or ""
    body = [l for l in text.splitlines() if l.strip()][:lines]
    print("    " + "\n    ".join(body) if body else "    (empty)")
    if len(text.splitlines()) > lines:
        print("    ...")


def choose_interactively(candidates, meta):
    """Let the user pick a candidate. Returns a record, None (skip) or 'quit'."""
    idx = 0
    while True:
        if candidates:
            print("  Matches:")
            for i, c in enumerate(candidates[:8], 1):
                marker = "*" if i - 1 == idx else " "
                print("   {}{}. {}".format(marker, i, describe(c)))
        else:
            print("  No matches found.")
        choice = ask("  [Enter]=use *, 1-8=pick, p=preview, s=search, n=skip, "
                     "a=auto for rest, q=quit").lower()
        if choice == "" and candidates:
            return candidates[idx]
        if choice.isdigit() and 1 <= int(choice) <= min(8, len(candidates)):
            idx = int(choice) - 1
            preview(candidates[idx])
            continue
        if choice == "p" and candidates:
            preview(candidates[idx])
        elif choice == "s":
            default_q = "{} {}".format(meta["artist"], meta["title"]).strip()
            query = ask("  Search query", default_q)
            try:
                candidates = manual_search(query)
            except Exception as e:
                print("  Search failed: {}".format(e))
            candidates.sort(key=lambda c: score_result(c, meta))
            idx = 0
        elif choice == "n":
            return None
        elif choice == "a":
            return "auto"
        elif choice == "q":
            return "quit"


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(
        description="Fetch timestamped lyrics from LRCLIB and embed them in audio files.")
    parser.add_argument("directory", nargs="?", help="folder with audio files "
                        "(default: prompt, suggesting the script's folder)")
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
    args = parser.parse_args()

    print("=" * 60)
    print(" AutoLyricEmbedder  -  synced lyrics from lrclib.net")
    print("=" * 60)

    interactive_setup = args.directory is None
    directory = args.directory or ask("Folder with audio files", script_dir)
    directory = os.path.abspath(os.path.expanduser(directory.strip('"')))
    if not os.path.isdir(directory):
        print("Not a folder: {}".format(directory))
        return 1

    if interactive_setup:
        args.recursive = ask_yes_no("Include subfolders?", args.recursive)
        args.overwrite = ask_yes_no("Overwrite lyrics that are already embedded?", args.overwrite)
        args.plain = ask_yes_no("Use plain (untimed) lyrics when no synced version exists?", True)
        args.lrc = ask_yes_no("Also save .lrc files next to the audio?", args.lrc)
        args.auto = not ask_yes_no("Confirm each match before writing?", True)

    files = collect_files(directory, args.recursive)
    if not files:
        print("No supported audio files found in {}".format(directory))
        return 0
    print("\nFound {} audio file(s) in {}\n".format(len(files), directory))

    stats = {"synced": 0, "plain": 0, "skipped": 0, "not_found": 0, "errors": 0}
    auto = args.auto

    for n, path in enumerate(files, 1):
        rel = os.path.relpath(path, directory)
        print("[{}/{}] {}".format(n, len(files), rel))
        try:
            meta = read_metadata(path)
        except Exception as e:
            print("  ! Could not read tags: {}".format(e))
            stats["errors"] += 1
            continue
        if not meta:
            print("  ! Unsupported or unreadable file")
            stats["errors"] += 1
            continue
        print("  Tags: {} - {} [{}] ({})".format(meta["artist"] or "?", meta["title"],
                                               meta["album"] or "-", fmt_duration(meta["duration"])))

        if not args.overwrite and has_lyrics(path):
            print("  - Already has lyrics, skipping (use overwrite to replace)")
            stats["skipped"] += 1
            continue

        try:
            candidates = find_lyrics(meta)
        except Exception as e:
            print("  ! LRCLIB request failed: {}".format(e))
            stats["errors"] += 1
            continue

        if auto:
            rec = candidates[0] if candidates else None
            if rec:
                print("  Match: {}".format(describe(rec)))
        else:
            rec = choose_interactively(candidates, meta)
            if rec == "quit":
                break
            if rec == "auto":
                auto = True
                rec = candidates[0] if candidates else None

        if not rec:
            print("  - No lyrics applied")
            stats["not_found" if not candidates else "skipped"] += 1
            continue
        if rec.get("instrumental"):
            print("  - Marked instrumental on LRCLIB, skipping")
            stats["skipped"] += 1
            continue

        lyrics, synced = rec.get("syncedLyrics"), True
        if not lyrics:
            if args.plain and rec.get("plainLyrics"):
                lyrics, synced = rec["plainLyrics"], False
            else:
                print("  - Only plain lyrics available, skipping")
                stats["not_found"] += 1
                continue

        if args.dry_run:
            print("  (dry run) would embed {} lyrics".format("synced" if synced else "plain"))
        else:
            try:
                write_lyrics(path, lyrics, synced, write_sylt=not args.no_sylt)
                if args.lrc:
                    write_lrc_file(path, lyrics)
            except Exception as e:
                print("  ! Failed to write: {}".format(e))
                stats["errors"] += 1
                continue
            print("  + Embedded {} lyrics{}".format("synced" if synced else "plain",
                                                   " (+ .lrc)" if args.lrc else ""))
        stats["synced" if synced else "plain"] += 1
        time.sleep(0.2)  # be polite to the free API

    print("\nDone. Synced: {synced}  Plain: {plain}  Skipped: {skipped}  "
          "Not found: {not_found}  Errors: {errors}".format(**stats))
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except KeyboardInterrupt:
        print("\nCancelled.")
        code = 130
    if len(sys.argv) == 1 and os.name == "nt":
        ask("\nPress Enter to exit")  # keep the window open when double-clicked
    sys.exit(code)
