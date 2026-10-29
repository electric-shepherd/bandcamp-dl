#!/usr/bin/env python3
"""Terminal UI for bandcamp_dl: paste the identity cookie, pick a folder
and a format, then watch the downloads.

Settings (not the cookie, unless you tick "Remember") are kept in
~/.config/bandcamp-dl/. Needs `textual` (pip install -r requirements.txt).
"""

import argparse
import json
import os
import threading
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.theme import Theme
from textual.widgets import (Button, Checkbox, DirectoryTree, Footer, Header,
                             Input, Label, ProgressBar, RichLog, Select)

import bandcamp_dl as bc

# "no choice" marker: Select.BLANK up to Textual 2.x, Select.NULL after.
NO_SELECTION = getattr(Select, "NULL", None) or Select.BLANK

CONFIG_DIR = bc.CONFIG_DIR
CONFIG_FILE = CONFIG_DIR / "config.json"
IDENTITY_FILE = bc.IDENTITY_FILE
clean_identity = bc.clean_identity

BROWSERS = [("Safari", "safari"), ("Chrome", "chrome"),
            ("Chromium", "chromium"), ("Brave", "brave"), ("Edge", "edge")]

FORMAT_LABELS = {
    "flac": "FLAC", "wav": "WAV", "vorbis": "Ogg Vorbis",
    "mp3-320": "MP3 320", "mp3-v0": "MP3 V0", "alac": "Apple Lossless",
    "aac-hi": "AAC", "aiff-lossless": "AIFF",
}

# Bandcamp's own blue on a dark ground, plus a light twin.
THEMES = [
    Theme(name="bandcamp", primary="#1DA0C3", secondary="#629AA9",
          accent="#F5A623", warning="#F5A623", error="#E2504C",
          success="#5BB974", background="#14181B", surface="#1C2226",
          panel="#232B30", foreground="#E6EDF0", dark=True),
    Theme(name="bandcamp-light", primary="#1287A8", secondary="#408294",
          accent="#C77700", warning="#C77700", error="#C62828",
          success="#2E7D32", background="#F6F8F9", surface="#FFFFFF",
          panel="#E3EBEE", foreground="#1B2327", dark=False),
]


def tilde(path):
    """/home/me/Music -> ~/Music, for display."""
    try:
        return "~/" + str(Path(path).relative_to(Path.home()))
    except ValueError:
        return str(path)


def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_config(cfg, identity=None):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=1, sort_keys=True))
    bc.save_identity(identity)


class FolderTree(DirectoryTree):
    """Directory browser that shows folders only, hiding dot-folders."""

    def filter_paths(self, paths):
        return [p for p in paths
                if not p.name.startswith(".") and p.is_dir()]


class BandcampTUI(App):
    TITLE = "bandcamp-dl"
    SUB_TITLE = "download your collection"

    CSS = """
    Screen { layout: vertical; }
    #top { height: 1fr; min-height: 18; }
    #settings { width: 46; border: round $primary; padding: 0 1; }
    #folder { border: round $primary; padding: 0 1; }
    #settings, #folder { border-title-color: $accent;
                         border-title-style: bold; }
    .field { color: $text-muted; margin-top: 1; }
    #cookie-row, #browser-row { height: auto; }
    #browser { width: 1fr; }
    #identity { width: 1fr; }
    #show { min-width: 8; width: 8; margin-left: 1; }
    #login-status { height: auto; margin-top: 1; }
    #flags Checkbox { width: 100%; }
    #dir-row { height: auto; }
    #output { width: 1fr; }
    #dir-row Button { min-width: 5; width: 5; margin-left: 1; }
    FolderTree { height: 1fr; margin-top: 1; background: $surface; }
    #bottom { height: 14; border: round $secondary; padding: 0 1;
              border-title-color: $accent; }
    #progress-row { height: 1; }
    #counts { width: auto; margin-left: 2; color: $text-muted; }
    #log { height: 1fr; background: $surface; }
    #actions { height: 3; align-horizontal: right; }
    #actions Button { margin-left: 1; }
    """

    BINDINGS = [
        Binding("ctrl+l", "check_login", "Check login"),
        Binding("ctrl+n", "dry_run", "Dry run"),
        Binding("ctrl+d", "download", "Download"),
        Binding("ctrl+s", "stop", "Stop"),
        Binding("f2", "next_theme", "Theme"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.stop_event = threading.Event()
        self.busy = False
        self.done = self.failed = 0
        self.lines = []  # (kind, text): replayed in the new colours
        for theme in THEMES:
            self.register_theme(theme)
        if self.cfg.get("theme") in self.available_themes:
            self.theme = self.cfg["theme"]
        else:
            self.theme = "bandcamp"

    # ---------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        cfg = self.cfg
        fmt_options = [(f"{FORMAT_LABELS[f]}  ({f})", f) for f in bc.FORMATS]
        try:
            identity = (os.environ.get("BANDCAMP_IDENTITY")
                        or IDENTITY_FILE.read_text().strip())
        except OSError:
            identity = ""
        out = cfg.get("output") or "~/Music/Bandcamp"

        yield Header()
        with Horizontal(id="top"):
            with VerticalScroll(id="settings") as v:
                v.border_title = "Settings"
                yield Label("identity cookie (paste: ctrl+v)",
                            classes="field")
                with Horizontal(id="cookie-row"):
                    yield Input(identity, placeholder="7%09abc...",
                                password=True, id="identity")
                    yield Button("Show", id="show")
                with Horizontal(id="browser-row"):
                    yield Select(BROWSERS, value=cfg.get("browser", "safari"),
                                 allow_blank=False, id="browser")
                    yield Button("Get from browser", id="get-cookie")
                yield Checkbox("Remember cookie on this computer",
                               bool(IDENTITY_FILE.exists()), id="remember")
                yield Label("", id="login-status")
                yield Label("format", classes="field")
                yield Select(fmt_options, value=cfg.get("format", "flac"),
                             allow_blank=False, id="format")
                yield Label("fallback if not offered", classes="field")
                yield Select(fmt_options, value=cfg.get("fallback")
                             or NO_SELECTION, prompt="(none)",
                             id="fallback")
                yield Label("parallel downloads", classes="field")
                yield Input(str(cfg.get("jobs", 3)), type="integer",
                            id="jobs")
                with Vertical(id="flags"):
                    yield Checkbox("Include hidden items",
                                   cfg.get("include_hidden", False),
                                   id="include_hidden")
                    yield Checkbox("Keep album zips",
                                   cfg.get("keep_zip", False), id="keep_zip")
                    yield Checkbox("Re-download everything (force)",
                                   False, id="force")
                yield Label("theme", classes="field")
                yield Select([(t, t) for t in sorted(self.available_themes)],
                             value=self.theme, allow_blank=False,
                             id="theme")
            with Vertical(id="folder") as v:
                v.border_title = "Download folder"
                with Horizontal(id="dir-row"):
                    yield Input(out, id="output",
                                placeholder="folder (created if missing)")
                    yield Button("↑", id="up", tooltip="parent folder")
                    yield Button("~", id="home", tooltip="home folder")
                yield FolderTree(self._browse_root(out), id="tree")
        with Vertical(id="bottom") as v:
            v.border_title = "Progress"
            with Horizontal(id="progress-row"):
                yield ProgressBar(id="progress", show_eta=True)
                yield Label("", id="counts")
            yield RichLog(id="log", markup=False, wrap=True)
            with Horizontal(id="actions"):
                yield Button("Check login", id="check")
                yield Button("Dry run", id="dry", variant="primary")
                yield Button("Download", id="go", variant="success")
                yield Button("Stop", id="stop", variant="error",
                             disabled=True)
        yield Footer()

    def on_mount(self):
        self.info("Paste your bandcamp.com 'identity' cookie (or Get from "
                  "browser), pick a folder, then Dry run or Download.")

    @staticmethod
    def _browse_root(out):
        """Nearest existing folder at or above `out`."""
        p = Path(out).expanduser()
        while not p.is_dir() and p != p.parent:
            p = p.parent
        return p if p.is_dir() else Path.home()

    # -------------------------------------------------------------- logging

    # log line kind -> (theme colour, bold)
    KINDS = {"info": (None, False), "ok": ("success", False),
             "warn": ("warning", False), "err": ("error", True),
             "head": ("primary", True)}

    def _color(self, name):
        theme = self.current_theme
        return getattr(theme, name, None) or theme.primary

    def _styled(self, kind, msg):
        color, bold = self.KINDS[kind]
        style = self._color(color) if color else ""
        return Text(msg, style=f"bold {style}" if bold else style)

    def _write(self, kind, msg):
        self.lines = self.lines[-1999:] + [(kind, msg)]
        self.query_one("#log", RichLog).write(self._styled(kind, msg))

    def _recolor_log(self):
        log = self.query_one("#log", RichLog)
        log.clear()
        for kind, msg in self.lines:
            log.write(self._styled(kind, msg))

    def info(self, msg):
        self._write("info", msg)

    def ok(self, msg):
        self._write("ok", msg)

    def warn(self, msg):
        self._write("warn", msg)

    def err(self, msg):
        self._write("err", msg)

    def head(self, msg):
        self._write("head", msg)

    def from_thread(self, fn, *a):
        """Run a UI call from a worker thread; harmless after quit."""
        try:
            self.call_from_thread(fn, *a)
        except RuntimeError:
            pass

    # ------------------------------------------------------------- settings

    def _args(self):
        identity = clean_identity(self.query_one("#identity", Input).value)
        if not identity:
            raise bc.BandcampError("paste the identity cookie first")
        fmt = self.query_one("#format", Select).value
        fallback = self.query_one("#fallback", Select).value
        formats = [fmt] + ([fallback] if fallback not in
                           (NO_SELECTION, fmt) else [])
        out = self.query_one("#output", Input).value.strip()
        if not out:
            raise bc.BandcampError("choose a download folder")
        try:
            jobs = max(1, min(8, int(self.query_one("#jobs", Input).value)))
        except ValueError:
            jobs = 3
        flag = lambda name: self.query_one(f"#{name}", Checkbox).value
        return argparse.Namespace(
            identity=identity, cookies=None, format=formats,
            output=Path(out).expanduser(), jobs=jobs, retries=3,
            include_hidden=flag("include_hidden"), keep_zip=flag("keep_zip"),
            force=flag("force"), limit=None)

    def _save(self, args):
        self.cfg.update(
            format=args.format[0],
            fallback=args.format[1] if len(args.format) > 1 else None,
            output=str(args.output), jobs=args.jobs,
            include_hidden=args.include_hidden, keep_zip=args.keep_zip,
            theme=self.theme)
        remember = self.query_one("#remember", Checkbox).value
        try:
            save_config(self.cfg, args.identity if remember else None)
        except OSError as e:
            self.warn(f"could not save settings: {e}")

    # --------------------------------------------------------------- events

    @on(Button.Pressed, "#show")
    def toggle_show(self, event):
        box = self.query_one("#identity", Input)
        box.password = not box.password
        event.button.label = "Hide" if not box.password else "Show"

    @on(Button.Pressed, "#get-cookie")
    def get_cookie(self, event):
        browser = self.query_one("#browser", Select).value
        self.cfg["browser"] = browser
        event.button.disabled = True
        self.info(f"reading the bandcamp.com cookie from {browser} "
                  "(macOS may ask to allow keychain access)…")
        self.read_browser_cookie(browser)

    @work(thread=True, exclusive=True, group="browser")
    def read_browser_cookie(self, browser):
        ui = self.from_thread
        try:
            identity = bc.browser_identity(browser)
        except Exception as e:
            ui(self.err, f"{browser}: {e}")
        else:
            ui(self._got_cookie, browser, identity)
        finally:
            ui(self._enable_get_cookie)

    def _got_cookie(self, browser, identity):
        self.query_one("#identity", Input).value = identity
        self.ok(f"got the cookie from {browser}; Check login to test it")

    def _enable_get_cookie(self):
        self.query_one("#get-cookie", Button).disabled = False

    @on(Input.Changed, "#identity")
    def tidy_identity(self, event):
        cleaned = clean_identity(event.value)
        if cleaned != event.value.strip() and cleaned:
            event.input.value = cleaned  # pasted a whole cookie string
        self.query_one("#login-status", Label).update("")

    @on(Select.Changed, "#theme")
    def pick_theme(self, event):
        if event.value != NO_SELECTION and event.value != self.theme:
            self.theme = event.value

    def watch_theme(self, theme):
        try:
            self.query_one("#theme", Select).value = theme
        except Exception:
            return  # not composed yet
        self._recolor_log()
        self.cfg["theme"] = theme
        try:
            save_config(self.cfg, self._remembered_identity())
        except OSError:
            pass

    def _remembered_identity(self):
        if not self.query_one("#remember", Checkbox).value:
            return None
        return clean_identity(self.query_one("#identity", Input).value) or None

    def action_next_theme(self):
        names = sorted(self.available_themes)
        i = names.index(self.theme) if self.theme in names else -1
        self.theme = names[(i + 1) % len(names)]
        self.notify(f"theme: {self.theme}", timeout=1.5)

    @on(FolderTree.DirectorySelected)
    def pick_folder(self, event):
        self.query_one("#output", Input).value = str(event.path)

    @on(Input.Submitted, "#output")
    def browse_typed(self, event):
        self.query_one("#tree", FolderTree).path = \
            self._browse_root(event.value)

    @on(Button.Pressed, "#up")
    def browse_up(self):
        tree = self.query_one("#tree", FolderTree)
        parent = Path(tree.path).parent
        tree.path = parent
        self.query_one("#output", Input).value = str(parent)

    @on(Button.Pressed, "#home")
    def browse_home(self):
        self.query_one("#tree", FolderTree).path = Path.home()

    @on(Button.Pressed, "#check")
    def action_check_login(self):
        self._start("check")

    @on(Button.Pressed, "#dry")
    def action_dry_run(self):
        self._start("dry")

    @on(Button.Pressed, "#go")
    def action_download(self):
        self._start("download")

    @on(Button.Pressed, "#stop")
    def action_stop(self):
        if self.busy and not self.stop_event.is_set():
            self.stop_event.set()
            self.warn("stopping: letting running downloads finish, "
                      "dropping the rest…")

    # ----------------------------------------------------------------- work

    def _start(self, mode):
        if self.busy:
            self.notify("already running", severity="warning")
            return
        try:
            args = self._args()
        except bc.BandcampError as e:
            self.err(str(e))
            return
        self._save(args)
        self.stop_event.clear()
        self.done = self.failed = 0
        self._set_busy(True)
        self.run_job(mode, args)

    def _set_busy(self, busy):
        self.busy = busy
        for b in ("#check", "#dry", "#go"):
            self.query_one(b, Button).disabled = busy
        self.query_one("#stop", Button).disabled = not busy

    def _login_status(self, text, kind):
        self.query_one("#login-status", Label).update(
            Text(text, style=f"bold {self._color(kind)}"))

    def _progress(self, total=None, advance=0):
        bar = self.query_one("#progress", ProgressBar)
        if total is not None:
            bar.update(total=total, progress=0)
        if advance:
            bar.advance(advance)
        total = bar.total or 0
        text = f"{self.done}/{int(total)}"
        if self.failed:
            text += f" · {self.failed} failed"
        self.query_one("#counts", Label).update(text)

    @work(thread=True, exclusive=True)
    def run_job(self, mode, args):
        ui = self.from_thread
        try:
            self._job(mode, args)
        except Exception as e:
            ui(self.err, f"error: {e}")
        finally:
            ui(self._set_busy, False)

    def _job(self, mode, args):
        ui = self.from_thread
        ui(self.head, f"— {mode} —")
        s = bc.make_session(args)
        try:
            fan_id, user = bc.get_fan_id(s)
        except bc.BandcampError:
            ui(self._login_status, "✗ login rejected", "error")
            raise
        ui(self._login_status, f"✓ logged in as {user}", "success")
        ui(self.ok, f"logged in as {user} (fan {fan_id})")
        if mode == "check":
            return

        ui(self.info, "fetching collection…")
        items = list(bc.iter_collection(s, fan_id))
        if args.include_hidden:
            items += list(bc.iter_collection(s, fan_id, "hidden_items"))
        ui(self.info, f"{len(items)} items in collection")

        if mode == "download":
            args.output.mkdir(parents=True, exist_ok=True)
        state = bc.load_state(args.output)
        todo, skipped = bc.build_todo(
            items, state, args, lambda m: ui(self.warn, m.strip()))
        ui(self.head, f"{len(todo)} to download, {skipped} skipped "
                      f"→ {tilde(args.output)}  [{', '.join(args.format)}]")
        ui(self._progress, len(todo))

        if mode == "dry":
            for _, label, _, _ in todo:
                ui(self.info, f"  {label}")
            return
        if not todo:
            ui(self.ok, "nothing new; all done")
            return

        def report(n, label, fmt, error):
            if error:
                self.failed += 1
                ui(self.err, f"✗ {label}: {error}")
            else:
                self.done += 1
                ui(self.ok, f"✓ {fmt:<13} {label}")
            ui(self._progress, None, 1)

        failed = bc.download_all(
            s, args, todo, state, report,
            log=lambda m: ui(self.warn, m.strip()), stop=self.stop_event)
        dropped = len(todo) - self.done - len(failed)
        summary = f"done: {self.done} downloaded, {len(failed)} failed"
        if dropped:
            summary += f", {dropped} not started (stopped)"
        ui(self.err if failed else self.head, summary)
        for label in failed:
            ui(self.err, f"  failed: {label}")


def main():
    app = BandcampTUI()
    app.run()
    if app.busy:
        # Quit mid-download: don't wait for the running files. Their
        # .part files are left behind and re-fetched next run.
        os._exit(130)


if __name__ == "__main__":
    main()
