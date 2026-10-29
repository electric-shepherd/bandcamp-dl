# bandcamp-dl

Downloads every purchase in your Bandcamp collection in the format you pick
(FLAC, WAV, Ogg Vorbis, MP3, ALAC, AAC, AIFF). Albums are unzipped into
`Artist/Album/`. Re-running only fetches what is new.

## Setup

    pip install -r requirements.txt      # requests (+ textual for the TUI)

On Debian/Ubuntu: `apt install python3-requests python3-textual`.

## Log in (borrow your browser session)

Bandcamp's login form has a captcha, so the script uses the cookie of a
browser that is already logged in. Log in to bandcamp.com in your browser,
then do this once:

    ./bandcamp_dl.py --login --from-browser safari    # or chrome
    ./bandcamp_dl.py --login                          # paste it instead

`--login` tests the cookie with Bandcamp and saves it in
`~/.config/bandcamp-dl/identity` (readable only by you); later runs need no
login options. `--logout` deletes it. Run `--login` again when Bandcamp
logs you out.

Ways to supply the cookie, first match wins: `--identity VALUE`,
`BANDCAMP_IDENTITY`, `--from-browser`, `--cookies cookies.txt`, the saved
login.

**`--from-browser`** (`safari`, `chrome`, `chromium`, `brave`, `edge`)
reads the cookie straight out of the browser, no copying:

* **Safari** (macOS): macOS only lets apps with *Full Disk Access* read
  Safari's cookies. Add your terminal app under System Settings → Privacy &
  Security → Full Disk Access, then open a new terminal window.
* **Chrome family** (macOS, Linux): the cookie is encrypted with a key kept
  in the keychain / desktop keyring. On macOS a dialog asks whether
  `security` may use "Chrome Safe Storage": click *Allow* (or *Always
  Allow*). Decryption uses the `openssl` command, which macOS ships. The
  most recently used profile wins; pick one with `--profile 'Profile 1'`.
  Windows Chrome is not supported (app-bound encryption); paste instead.

**By hand**: in the browser on bandcamp.com open DevTools →
Storage/Application → Cookies → `https://bandcamp.com` and copy the value
of `identity`. Or export a cookies.txt with a browser extension and pass
`--cookies cookies.txt`.

Treat the cookie like a password; it is your logged-in session.

## Terminal UI

    ./bandcamp_tui.py

![TUI](docs/tui.png)

* **Cookie** — paste the `identity` value into the box (ctrl+v, or your
  terminal's paste). Pasting `identity=…`, a quoted value or a whole
  `Cookie:` header also works; it is trimmed to the value. *Show* reveals
  it. Or pick your browser and press *Get from browser* (same rules as
  `--from-browser` above). Tick *Remember* to keep it in `~/.config/bandcamp-dl/identity`
  (mode 0600); otherwise it is not written anywhere. `BANDCAMP_IDENTITY`
  pre-fills it.
* **Folder** — type a path (created if missing; Enter browses there) or
  pick one in the tree; `↑` goes to the parent, `~` home.
* **Format** — a first choice and an optional fallback, plus parallel
  downloads, hidden items, keep zips and force.
* **Run** — *Check login* (ctrl+l), *Dry run* (ctrl+n), *Download*
  (ctrl+d), *Stop* (ctrl+s; running files finish, the rest are dropped).
  Progress bar with ETA, coloured log (done / retry / failed).
* **Theme** — pick one in the settings or cycle with F2; `bandcamp`
  (default) and `bandcamp-light` plus Textual's built-ins (nord, gruvbox,
  dracula, tokyo-night, catppuccin, solarized, …). Also in the ctrl+p
  palette.

Settings and theme are saved in `~/.config/bandcamp-dl/config.json`.
Same state file as the CLI, so the two can be mixed.

## Command line

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

## Tests

    python3 -m unittest discover -s tests

Offline: they build fake Safari and Chrome cookie stores in the real file
formats and decrypt them (needs `openssl`).
