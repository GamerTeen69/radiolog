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
- **CSV export** that opens cleanly in Excel.
- **Handles Polish and other non-English titles**, including stations that send
  Windows-1250 text instead of UTF-8.
- **Survives dropped connections**: stream mode reconnects automatically.
- **No server, no account**: everything stays in one local file.

## Requirements

- Python 3.8 or newer (the Windows installer can install it for you)
- Windows 10/11, Linux or macOS
  - System mode: Windows or Linux (`playerctl` on Linux)
  - Stream mode: any OS

## Install

- **Windows:** double-click `install.bat`
- **Linux / macOS:** run `./install.sh` in this folder

The installer sets up everything, including the Windows components for system mode,
and adds the `radiolog` command to your PATH. Then open a **new** terminal window.

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

## Where the data lives

All songs are stored in a single SQLite database file. **The first time you start radiolog, it asks
where to keep it.** Press Enter to use the default location in your user folder:

- Windows: `C:\Users\<you>\.radiolog.db`
- Linux / macOS: `~/.radiolog.db`

You can also type any folder (for example `D:\Music\radiolog`) or a full file path.

> ⚠️ **Choose a location you can write to without administrator rights**, such as your user folder,
> Documents or another drive. Folders like `C:\Program Files`, `C:\Windows` or the root of `C:\`
> (on Linux: `/usr`, `/opt`, `/etc`) need administrator rights. If you choose one of those, radiolog
> must be run **elevated every time**: from a terminal opened with *Run as administrator*
> (Linux/macOS: with `sudo`). radiolog checks the location and warns you before saving it.

Change the location later (your existing history is moved for you):

```
radiolog config                       # show where the history is and whether it's writable
radiolog config --db D:\Music\radiolog  # move it somewhere else
radiolog config --reset               # ask again on next start
```

The setting itself is saved in `%APPDATA%\radiolog\config.json` (Linux: `~/.config/radiolog/`,
macOS: `~/Library/Application Support/radiolog/`). For a one-off override, set the `RADIOLOG_DB`
environment variable. Uninstalling radiolog keeps your history.

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

**"radiolog can't write to its database"**: the database is in a location that needs administrator
rights. Run radiolog from a terminal opened with *Run as administrator*, or move the history
somewhere you can write to with `radiolog config --db NEW_LOCATION`.

## Update / Uninstall

- **Update:** replace the files and run the installer again.
- **Uninstall:** double-click `uninstall.bat` (Windows) or run `./install.sh --uninstall`.

## How it works

- **System mode** asks the operating system's media session for the current artist and title
  every few seconds (Windows Global System Media Transport Controls via WinRT; MPRIS on Linux).
- **Stream mode** opens the stream with the `Icy-MetaData: 1` header and reads the `StreamTitle`
  blocks that Shoutcast/Icecast servers insert between chunks of audio. The audio itself is discarded.
- A new entry is saved only when the artist/title changes.
