# AutoLyricEmbedder

Fetches synced (timestamped) lyrics from [LRCLIB](https://lrclib.net) and embeds them into the metadata of the audio files in a folder.

## Requirements

- Python 3.7+
- [mutagen](https://github.com/quodlibet/mutagen) – reading/writing audio tags
- [rich](https://github.com/Textualize/rich) – terminal UI (colors, tables, progress bars)

```
pip install -r requirements.txt
```

## Usage

Interactive mode (prompts for folder and options):

```
python autolyricembedder.py
```

Non-interactive:

```
python autolyricembedder.py "D:\Music" --auto --lrc
```

| Option | Description |
|---|---|
| `-r`, `--recursive` | Include subfolders |
| `-o`, `--overwrite` | Replace lyrics that are already embedded |
| `-a`, `--auto` | Use the best match without asking |
| `--lrc` | Also write `.lrc` files next to the audio |
| `--plain` | Use untimed lyrics when no synced version exists |
| `--no-sylt` | Don't write the ID3 `SYLT` frame (MP3/WAV/AIFF) |
| `--dry-run` | Show what would happen without writing anything |

### LRCLIB client identification (optional)

LRCLIB rate-limits requests per client. Identifying yourself raises the limit:

| Client | Approx. limit |
|---|---|
| Generic / anonymous | ~45 req/min |
| `User-Agent` naming the app (default) | ~90 req/min |
| Registered App ID (`Lrclib-App-Id`) | ~180 req/min |

Get a free App ID by registering a client-side app at <https://lrclib.net/developers>, then either answer the optional setup question in interactive mode (it can save the settings to `~/.autolyricembedder.json`), or pass:

| Option | Env variable | Description |
|---|---|---|
| `--user-agent UA` | `LRCLIB_USER_AGENT` | Full User-Agent, or just an email/URL added as contact info |
| `--app-id ID` | `LRCLIB_APP_ID` | Registered App ID |
| `--api-key KEY` | `LRCLIB_API_KEY` | API key (server-side apps only; never saved to disk) |
| `--save-client` | | Save `--user-agent` / `--app-id` for next time |
| `--reset-client` | | Forget saved identification |

Command-line options override environment variables, which override the saved config. Requests are sent one at a time with a short gap, and `429 Too Many Requests` responses are retried after the server's `Retry-After` delay.

When confirming matches interactively: `Enter` uses the selected match, `1-8` picks another, `p` previews, `s` searches manually, `n` skips, `a` switches to auto for the rest, `q` quits.

## How it works

1. Reads title, artist, album and duration from each file's tags (falls back to `Artist - Title` filenames).
2. Looks the song up on LRCLIB (`/api/get`, then `/api/search`), preferring synced lyrics and the closest duration.
3. Writes the lyrics into the file:

| Format | Tag |
|---|---|
| MP3 / WAV / AIFF | ID3 `USLT` (LRC text) + `SYLT` |
| FLAC / OGG / Opus | `LYRICS` |
| M4A / MP4 | `©lyr` |
| WMA | `WM/Lyrics` |
