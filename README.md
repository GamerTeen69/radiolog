# radiolog

**A small terminal tool that remembers every song played on your computer, built first and foremost for internet radio.**

Leave internet radio running in the background, and radiolog quietly writes down each new
*Artist — Title* with the date and time it was heard. Later you can look back and find
"that song from yesterday evening" in seconds.

Radio is where it shines, since stations never give you a history of what they played, but it isn't
limited to radio. radiolog follows whatever your system reports as "now playing", so it works
with practically any player: browser radio sites, YouTube, Spotify, VLC, foobar2000, and more.

```
Saving to C:\Users\you\.radiolog.db
Watching the system media session every 5s   (Ctrl+C to stop)

── Discovered ──────────────────────────────

[2026-10-19 01:22:16] Proletaryat — Hej naprzód marsz
[2026-10-19 01:26:03] Dżem — Wehikuł czasu
```

**Contents:** [Features](#features) · [Requirements](#requirements) · [Install](#install) ·
[Usage](#usage) · [Clear the history](#clear-the-history) · [Search or download](#search-or-download-a-song) · [Audio formats](#audio-formats) ·
[Setup and settings](#setup-and-settings) · [Troubleshooting](#troubleshooting) ·
[Update / Uninstall](#update--uninstall)

## Features

- **Made for internet radio**: catches every song change on a station, even when the radio
  only sends one combined "Artist - Title" line, and splits it into artist and title.
- **Universal: works with any player**
  - **System mode**: reads what's "now playing" on your computer, the same info Windows shows
    in the volume popup (Linux: any MPRIS player via `playerctl`). One command covers browser
    radio, YouTube, Spotify, VLC and any other player that reports the current song.
  - **Stream mode**: connects directly to a Shoutcast/Icecast radio stream and reads the song
    titles the station broadcasts. Works on any OS, with any player, or with no player at all.
- **Full history with timestamps**: every song is saved with the date and time it was
  first discovered; repeats of the same song are skipped.
- **Search and filter**: by day, time range or text (`--today`, `--since 3h`, `--search queen`).
- **Find it again**: open any logged song in a YouTube, YouTube Music or Spotify search with one command.
- **Download in the best quality available** with a ready-made [yt-dlp](https://github.com/yt-dlp/yt-dlp)
  command: best audio stream with no re-encoding, cover art and tags (or the full video).
  Prefer **MP3** or **M4A** for older devices? Set it once with `radiolog config --format`.
  **Batch download** whole ranges, a day's listening or search results, with no duplicates.
- **CSV export** that opens cleanly in Excel.
- **Clear the history** completely or partly (chosen songs, older than N days, matching text),
  with a confirmation and an automatic backup.
- **Handles Polish and other non-English titles**, including stations that send
  Windows-1250 text instead of UTF-8.
- **Survives dropped connections**: stream mode reconnects automatically.
- **No server, no account**: everything stays in one local file.

## Requirements

- Windows 10/11, Linux or macOS
- Python 3.8 or newer (the Windows installer can install it for you)
- For recording:
  - System mode: Windows, or Linux with `playerctl`
  - Stream mode: any OS
- For downloads: [yt-dlp](https://github.com/yt-dlp/yt-dlp) (installed automatically) and,
  recommended, [ffmpeg](https://ffmpeg.org) for cover art, tags, MP3 and full-quality video
  (the installer offers it)

## Install

- **Windows:** double-click `install.bat`
- **Linux / macOS:** run `./install.sh` in this folder

The installer sets up everything: the Windows components for system mode, yt-dlp for downloads,
and the `radiolog` command on your PATH. It also offers to install ffmpeg (recommended).

Then open a **new** terminal window and run any command, for example `radiolog show`. The first
start asks where to store your song history and your downloads; see [First-time setup](#first-time-setup).

## Usage

### Record

```
radiolog watch --system                          # follow your media player
radiolog watch --system --player chrome          # only follow one app
radiolog watch --url http://stream.example.com/radio.mp3
```

Stop with **Ctrl+C**.

### Browse the history

```
radiolog show                                    # last 50 songs
radiolog show --today
radiolog show --since 3h
radiolog show --since yesterday
radiolog show --since "2026-10-18 20:00" --until "2026-10-18 23:00"
radiolog show --search dżem
radiolog show --today --csv today.csv            # export to a spreadsheet
```

| Option | Meaning |
|---|---|
| `--today` | songs from today only |
| `--since`, `--until` | `30m`, `3h`, `2d`, `1w`, `today`, `yesterday`, `20:00`, `2026-10-18 20:00` |
| `--search TEXT` | match artist or title |
| `-n`, `--limit N` | maximum number of songs |
| `--csv FILE` | save the selection as CSV instead of printing |
| `--no-source` | hide the station / player name |

Every song has a number (`#12`), which the commands below use.

### Clear the history

Delete everything, chosen songs, or a time range:

```
radiolog clear                     # delete the whole history
radiolog clear 12                  # delete song #12
radiolog clear 3 5 8-12            # delete several songs
radiolog clear --today             # delete today's songs
radiolog clear --until 30d         # delete songs older than 30 days
radiolog clear --search "ad break" # delete entries matching a text (e.g. adverts, jingles)
```

radiolog shows what will be deleted and asks you to type **yes**. Before deleting, it saves a backup
copy next to the database (`radiolog-backup-<date>.db`), so a mistake can be undone: close radiolog
and copy the backup over the database file (its location is shown by `radiolog config`).
Downloaded songs and settings are never touched.

`-y` skips the question (needed when running from a script) and `--no-backup` skips the backup.
Old backups aren't removed automatically; delete them when you no longer need them.

### Search or download a song

The easiest way is the interactive menu: pick a song from the list, then choose what to do.

```
radiolog pick                      # last 20 songs (accepts the same filters as show)
radiolog pick --today --search queen
```
```
  #11  2026-10-19 01:22:16  Proletaryat — Hej naprzód marsz
  #12  2026-10-19 01:26:03  Dżem — Wehikuł czasu

Tip: pick several at once, e.g.  3 5 8-12  or  all

Song number(s) [12] (q to quit): 12

  ♪ Dżem - Wehikuł czasu
  [y] search on YouTube      [m] YouTube Music     [s] Spotify
  [d] download audio (best)  [v] download video    [c] show yt-dlp command
  [b] back to the list       [q] quit
```

Press **Enter** to pick the latest song. After an action you stay in the menu; press **Enter** or
**b** to go back to the list.

Or directly, by number (leave the number out to use the last song):

```
radiolog search 12                 # YouTube search in your browser
radiolog search 12 --ytmusic       # YouTube Music
radiolog search 12 --spotify       # Spotify
radiolog download                  # the last song
radiolog download 12               # best-quality audio
radiolog download 12 --video       # best-quality video (saved as "… (video).mkv")
radiolog download 12 --format mp3  # as .mp3 this time (see Audio formats below)
radiolog download 12 --print-cmd   # just print the yt-dlp command to copy
radiolog download 12 --dir D:\Music
```

### Batch download

Download many songs at once: several numbers, ranges, or everything matching a filter.

```
radiolog download 3 5 7            # several songs
radiolog download 10-25            # a range
radiolog download 3 8-12 20        # mixed
radiolog download --today          # everything heard today
radiolog download --since 3h --video
radiolog download --search queen -y   # -y: don't ask for confirmation
```

In `radiolog pick` you can also type several numbers (`3 5 8-12`) or `all`.

A batch download shows the list and asks once before starting. Songs played several times are
downloaded once, and songs already in your download folder are skipped (use `--force` to download
them again). At the end you get a summary of what was downloaded, skipped, not found on YouTube,
or failed.
Ctrl+C stops after the current song.

Downloads go to the folder you chose during the [first-time setup](#first-time-setup)
(default: `Music\radiolog` in your user folder). Change it with `radiolog config --downloads FOLDER`,
or use `--dir FOLDER` for a single download.

### Audio formats

By default radiolog keeps the audio exactly as YouTube streams it, **without re-encoding**, so no
quality is lost. That's the best you can get from YouTube. With ffmpeg installed it also adds the
title, artist and cover art. Choose another format if a device of yours can't play the default:

| Format | File | Quality | Plays on |
|---|---|---|---|
| `best` *(default)* | usually `.opus` | Best: YouTube's top stream (~160 kbps Opus), not converted | VLC, foobar2000, browsers, Android, Windows 10/11, most modern apps |
| `opus` | `.opus` | Same as `best` in practice | same as above |
| `m4a` | `.m4a` (AAC) | Very good: YouTube's own AAC stream (~128 kbps), not converted | Everything, including iPhone, iTunes and Apple Music |
| `mp3` | `.mp3` | Good: converted at the highest MP3 quality (VBR V0); converting loses a little | Everything, including old car stereos and MP3 players |

```
radiolog config --format mp3        # from now on
radiolog download 12 --format m4a   # just this time
```

`mp3` needs ffmpeg (the installer offers it). Without ffmpeg you get the original format instead.
Converting never improves quality: an MP3 made from YouTube's stream can't sound better than the
stream itself. Pick `mp3` for compatibility, not for quality.

Already-downloaded songs are skipped per format, so switching to `mp3` downloads MP3 versions of
songs you only have as `.opus`.

**Video** (`--video` or **[v]**) takes the best video and best audio streams and joins them into
`Artist - Title (video).mkv`, again without re-encoding.

**Without ffmpeg**, files are saved exactly as YouTube streams them: still the best quality, but
without cover art and tags, `mp3` falls back to the original format, and video may be lower
resolution. Install it with `winget install Gyan.FFmpeg` (Linux: `sudo apt install ffmpeg`).

### Notes

Downloads use the first YouTube search result for *Artist - Title*. Please respect copyright and
YouTube's terms in your country.

## Setup and settings

### First-time setup

The first time you run any radiolog command, it asks two questions. Press **Enter** to accept
the default shown in brackets.

| Step | Question | Default (Windows) | Default (Linux / macOS) |
|---|---|---|---|
| 1 | Where to store your **song history** (database file) | `C:\Users\<you>\.radiolog.db` | `~/.radiolog.db` |
| 2 | Where to save **downloaded songs** | `C:\Users\<you>\Music\radiolog` | `~/Music/radiolog` |

You can type a folder (`D:\Music\radiolog`) or, for the history, a full file path
(`D:\Music\history.db`). Folders are created if they don't exist. Each user on the computer gets
their own defaults and settings. If you already have a history file, setup shows how many songs it
holds and offers to move it if you choose a new place.

If you set radiolog up with a version that didn't ask about downloads yet, it asks that question
once, right before your first download.

> ⚠️ **Choose locations you can write to without administrator rights**, such as your user folder,
> Documents or another drive. Folders like `C:\Program Files`, `C:\Windows` or the root of `C:\`
> (on Linux: `/usr`, `/opt`, `/etc`) need administrator rights. If you choose one of those, radiolog
> must be run **elevated every time** it uses that location: from a terminal opened with
> *Run as administrator* (Linux/macOS: with `sudo`). radiolog tests every location before saving it,
> and if it can't write there it explains why and lets you pick another place or keep it anyway.

### Changing settings: `radiolog config`

Run it without options to see your current settings:

```
> radiolog config
Database:      C:\Users\you\.radiolog.db
Songs logged:  248
Writable:      yes
Downloads:     C:\Users\you\Music\radiolog
Audio format:  best  (the original YouTube stream, no conversion (usually .opus, best quality))
Settings file: C:\Users\you\AppData\Roaming\radiolog\config.json
```

| Option | What it does |
|---|---|
| *(none)* | Show where the history and downloads are, the audio format, how many songs are logged, and whether the history location is writable |
| `--db LOCATION` | Store the song history somewhere else: a folder or a full file path. Your existing history is moved there (radiolog asks first). If a history file already exists at the new place, radiolog switches to it and doesn't move anything. |
| `--downloads FOLDER` | Save future downloads in this folder. Files you already downloaded stay where they are. |
| `--format FORMAT` | Audio format for downloads: `best`, `opus`, `m4a` or `mp3`. See [Audio formats](#audio-formats). |
| `--reset` | Forget all settings (history location, download folder, audio format), so the first-time setup runs again on the next start. Your history file and downloaded songs are **not** deleted. |

The audio format is set only here (default `best`); the first-time setup doesn't ask about it.
Both locations are checked for write access. If one needs administrator rights, radiolog warns you
and asks before saving it.

```
radiolog config                               # show current settings
radiolog config --db D:\Music\radiolog         # move the history to D:\Music\radiolog\radiolog.db
radiolog config --db "D:\My Music\songs.db"    # use a specific file name (quotes for spaces)
radiolog config --downloads D:\Music           # change the download folder
radiolog config --format mp3                  # download songs as .mp3 from now on
radiolog config --db D:\Radio --downloads D:\Radio\songs   # both at once
radiolog config --reset                       # run the first-time setup again
```

**Good to know**
- Settings are stored per user in `%APPDATA%\radiolog\config.json` (Linux: `~/.config/radiolog/`,
  macOS: `~/Library/Application Support/radiolog/`).
- `radiolog download --dir FOLDER` saves into another folder for one download without changing the setting.
- The `RADIOLOG_DB` environment variable overrides the history location for that run only.
- Uninstalling radiolog keeps your history and your downloads.

## Troubleshooting

**"nie znaleziono Python" / "Python was not found"**: Windows' Microsoft Store placeholder is
answering instead of real Python. Run `install.bat`, which offers to install Python. To silence the
placeholder, go to *Settings → Apps → Advanced app settings → App execution aliases* and switch off
`python.exe` and `python3.exe`.

**`radiolog` is not recognized**: open a new terminal window after installing. If it still isn't
found, run the installer again.

**"... is a web page, not an audio stream"**: you gave stream mode a website address. For radio
you play in the browser, use `radiolog watch --system`, or find the station's direct stream link
(often ending in `.mp3`, `.aac`, `.pls` or `.m3u`).

**System mode shows nothing**: your player doesn't publish song info. Use stream mode with the
station's stream URL instead.

**"A Windows component is missing"**: run `install.bat` again.

**Download fails with an error**: YouTube changes often; updating yt-dlp usually fixes it.
Run the installer again, or `py -m pip install --user -U "yt-dlp[default]"`.

**"Unable to embed thumbnail" / "module mutagen was not found"**: versions before 1.5 installed
yt-dlp without its helper for cover art, so `.opus` downloads were reported as failed even though
the song was saved. Run the installer from the latest version.

**I chose `mp3` but got `.opus` files**: converting to MP3 needs ffmpeg. Install it
(`winget install Gyan.FFmpeg`), open a new terminal and download again.

**"Not found on YouTube"**: YouTube's search returned nothing for *Artist - Title*. radiolog
already retries with a simpler search (without dashes, apostrophes and other punctuation) before
giving up. Look the song up with `radiolog search NUMBER`. If it's on YouTube under a different
name, download it yourself: `yt-dlp -x "VIDEO_LINK"`. In a batch, these songs are listed under
*not found*.

**`.png` / `.webp` files next to my songs**: those are cover images that versions before 1.5
couldn't embed. radiolog now removes such leftovers when it downloads or skips that song; you can
also just delete them.

**I cleared songs by mistake**: every `radiolog clear` saves a backup first. Stop `radiolog watch`
if it's running, then copy the newest `radiolog-backup-….db` (in the same folder as your database,
see `radiolog config`) over the database file.

**The download is the wrong song**: radiolog uses the first YouTube result for *Artist - Title*.
Check with `radiolog search NUMBER` first, or download that video yourself with yt-dlp.

**"radiolog can't write to its database"**: the database is in a location that needs administrator
rights. Run radiolog from a terminal opened with *Run as administrator*, or move the history
somewhere you can write to with `radiolog config --db NEW_LOCATION`.

## Update / Uninstall

- **Update:** download the latest version from GitHub (*Code → Download ZIP*), unzip it over the
  old folder and run the installer again. Your history, downloads and settings are kept.
- **Uninstall:** double-click `uninstall.bat` (Windows) or run `./install.sh --uninstall`.

## Releases (for maintainers)

Releases are published automatically by GitHub Actions (`.github/workflows/release.yml`):
bump `version` in `pyproject.toml`, optionally add `release-notes/<version>.md` with the
description, then commit and push to `main`. GitHub creates the tag `v<version>`, the release, and
attaches `radiolog-<version>.zip`. If that version is already released, nothing happens.

## How it works

- **System mode** asks the operating system's media session for the current artist and title
  every few seconds (Windows Global System Media Transport Controls via WinRT; MPRIS on Linux).
- **Stream mode** opens the stream with the `Icy-MetaData: 1` header and reads the `StreamTitle`
  blocks that Shoutcast/Icecast servers insert between chunks of audio. The audio itself is discarded.
- A new entry is saved only when the artist/title changes.
- **Search** opens a YouTube, YouTube Music or Spotify search URL for *Artist - Title* in your browser.
- **Download** runs yt-dlp with `ytsearch1:"Artist - Title"`, picks the best stream for the chosen
  format, and (with ffmpeg) extracts the audio, adds tags and cover art. If the search finds
  nothing, it retries once with a plain "Artist Title" query. Afterwards radiolog checks that the
  file really exists before reporting success. `--print-cmd` shows the exact command.
