# AutoLyricEmbedder

Fetches synced (timestamped) lyrics from [LRCLIB](https://lrclib.net) and embeds them into the metadata of the audio files in a folder.

## Requirements

- Python 3.7+
- [mutagen](https://github.com/quodlibet/mutagen)

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
