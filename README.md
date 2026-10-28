# bandcamp-dl

Downloads every purchase in your Bandcamp collection in the format you pick
(FLAC, WAV, Ogg Vorbis, MP3, ALAC, AAC, AIFF). Albums are unzipped into
`Artist/Album/`. Re-running only fetches what is new.

## Setup

    pip install -r requirements.txt      # only needs `requests`

## Log in (borrow your browser session)

Bandcamp's login form has a captcha, so the script uses the cookie of a
browser that is already logged in. Either:

* **identity cookie only** — in the browser on bandcamp.com open
  DevTools → Storage/Application → Cookies → `https://bandcamp.com`, copy the
  value of `identity`, then

      export BANDCAMP_IDENTITY='7%09abc...'

* **cookies.txt** — export with a "cookies.txt" browser extension and pass
  `--cookies cookies.txt`.

Treat the cookie like a password; it is your logged-in session.

## Use

    ./bandcamp_dl.py -f flac -o ~/Music/Bandcamp --dry-run   # see the list
    ./bandcamp_dl.py -f flac -o ~/Music/Bandcamp             # download
    ./bandcamp_dl.py -f flac,wav                             # FLAC, else WAV
    ./bandcamp_dl.py -f ogg --include-hidden                 # hidden items too

| `-f` value | Bandcamp format |
|---|---|
| `flac` | FLAC |
| `wav` | WAV |
| `ogg` / `vorbis` | Ogg Vorbis |
| `mp3` / `mp3-320` | MP3 320 |
| `v0` / `mp3-v0` | MP3 V0 |
| `alac` | Apple Lossless |
| `aac` / `m4a` / `aac-hi` | AAC |
| `aiff` / `aiff-lossless` | AIFF |

Other options: `-j N` parallel downloads (default 3), `--keep-zip`,
`--force` (re-download), `--limit N`, `--retries N`.

Progress lives in `<output>/.bandcamp-dl-state.json`; items already done in an
acceptable format are skipped. Delete the file or use `--force` to start over.
Exit status is 1 if any item failed (it keeps going and lists them).

Items without a download link (pre-orders, some subscription items) are
listed and skipped.
