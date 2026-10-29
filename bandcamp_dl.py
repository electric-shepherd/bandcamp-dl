#!/usr/bin/env python3
"""Download your whole Bandcamp collection in one chosen format.

Bandcamp has no public login API (the login form has a captcha), so the
script borrows the session of a browser that is already logged in: either
a Netscape cookies.txt export or just the value of the `identity` cookie.

Flow:
  1. /api/fan/2/collection_summary          -> fan_id (proves the login works)
  2. /api/fancollection/1/collection_items   -> every purchase + its
     redownload page URL (paged with older_than_token)
  3. redownload page                         -> data-blob JSON listing a
     download link per format
  4. statdownload                            -> fresh signed file URL
  5. stream the file, unzip albums into Artist/Album/
"""

import argparse
import html
import http.cookiejar
import json
import os
import random
import re
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

BASE = "https://bandcamp.com"
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64; rv:128.0) "
              "Gecko/20100101 Firefox/128.0")

# Bandcamp's own format keys, plus friendlier names people actually type.
FORMATS = ["mp3-v0", "mp3-320", "flac", "aac-hi", "vorbis", "alac", "wav",
           "aiff-lossless"]
ALIASES = {"mp3": "mp3-320", "v0": "mp3-v0", "ogg": "vorbis",
           "aac": "aac-hi", "m4a": "aac-hi", "aiff": "aiff-lossless"}

STATE_FILE = ".bandcamp-dl-state.json"


class BandcampError(Exception):
    pass


def resolve_formats(spec):
    """'flac,wav' -> ['flac', 'wav'] in preference order."""
    out = []
    for name in spec.split(","):
        name = name.strip().lower()
        name = ALIASES.get(name, name)
        if name not in FORMATS:
            raise argparse.ArgumentTypeError(
                f"unknown format {name!r}; choose from "
                f"{', '.join(FORMATS + sorted(ALIASES))}")
        out.append(name)
    return out


def safe_name(text, limit=120):
    """Make a string usable as a file/dir name on Linux, macOS and Windows."""
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", text or "Unknown").strip()
    text = text.rstrip(". ") or "Unknown"
    return text[:limit]


def make_session(args):
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    if args.cookies:
        jar = http.cookiejar.MozillaCookieJar(args.cookies)
        jar.load(ignore_discard=True, ignore_expires=True)
        s.cookies.update(jar)
    identity = args.identity or os.environ.get("BANDCAMP_IDENTITY")
    if identity:
        s.cookies.set("identity", identity, domain=".bandcamp.com")
    if "identity" not in s.cookies.keys():
        raise BandcampError(
            "no 'identity' cookie: pass --cookies cookies.txt, --identity, "
            "or set BANDCAMP_IDENTITY (see README)")
    return s


def get_fan_id(s):
    r = s.get(f"{BASE}/api/fan/2/collection_summary", timeout=30)
    r.raise_for_status()
    data = r.json()
    if "fan_id" not in data:
        raise BandcampError(
            "login rejected by Bandcamp (identity cookie expired or wrong): "
            f"{data.get('error_message', data)}")
    user = data.get("collection_summary", {}).get("username", "?")
    return data["fan_id"], user


def iter_collection(s, fan_id, endpoint="collection_items"):
    """Yield (item, redownload_url) for every item in the collection."""
    token = f"{int(time.time())}::a::"
    while True:
        r = s.post(f"{BASE}/api/fancollection/1/{endpoint}",
                   json={"fan_id": fan_id, "older_than_token": token,
                         "count": 100},
                   timeout=60)
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            raise BandcampError(f"{endpoint}: {data.get('error_message')}")
        urls = data.get("redownload_urls") or {}
        for item in data.get("items", []):
            key = f"{item.get('sale_item_type')}{item.get('sale_item_id')}"
            yield item, urls.get(key)
        if not data.get("more_available") or not data.get("last_token"):
            return
        token = data["last_token"]


def page_blob(s, url):
    """Return the JSON in <div id="pagedata" data-blob="..."> of a page."""
    r = s.get(url, timeout=60)
    r.raise_for_status()
    m = re.search(r'id="pagedata"[^>]*?data-blob="([^"]*)"', r.text)
    if not m:
        raise BandcampError(f"no pagedata on {url} (cookie expired?)")
    return json.loads(html.unescape(m.group(1)))


def pick_download(blob, formats):
    """Return (format, url) for the first preferred format on offer."""
    items = blob.get("download_items") or blob.get("digital_items") or []
    if not items:
        raise BandcampError("download page lists no items")
    downloads = items[0].get("downloads") or {}
    for fmt in formats:
        if fmt in downloads and downloads[fmt].get("url"):
            return fmt, downloads[fmt]["url"]
    raise BandcampError(
        f"none of {formats} offered (has: {', '.join(downloads) or 'none'})")


def fresh_url(s, url):
    """Ask statdownload for the real file URL; fall back to the original.

    The download link on the page is signed and sometimes stale; the stat
    call either returns download_url (ready) or retry_url (re-signed).
    """
    stat = url.replace("/download/", "/statdownload/", 1)
    stat += f"&.vrs=1&.rand={random.randint(10**8, 10**9)}"
    try:
        r = s.get(stat, timeout=60)
        m = re.search(r"\{.*\}", r.text, re.S)
        info = json.loads(m.group(0)) if m else {}
    except (requests.RequestException, ValueError):
        return url
    return info.get("download_url") or info.get("retry_url") or url


def filename_from(resp, fallback):
    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd)
    if m:
        return safe_name(requests.utils.unquote(m.group(1)), 200)
    m = re.search(r'filename="?([^";]+)"?', cd)
    if m:
        return safe_name(m.group(1).encode("latin-1", "replace")
                         .decode("utf-8", "replace"), 200)
    return fallback


def _stderr(msg):
    print(msg, file=sys.stderr)


def download_file(s, url, dest_dir, fallback_name, retries, log=_stderr):
    dest_dir.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, retries + 1):
        try:
            with s.get(fresh_url(s, url), stream=True, timeout=120) as r:
                r.raise_for_status()
                if "text/html" in r.headers.get("Content-Type", ""):
                    raise BandcampError("got an HTML page instead of a file")
                path = dest_dir / filename_from(r, fallback_name)
                part = path.with_name(path.name + ".part")
                with open(part, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
                part.replace(path)
                return path
        except (requests.RequestException, BandcampError) as e:
            if attempt == retries:
                raise
            log(f"    retry {attempt}/{retries - 1}: {e}")
            time.sleep(5 * attempt)


def unpack(path, dest_dir):
    """Albums arrive as a zip; extract next to it and drop the zip."""
    if not zipfile.is_zipfile(path):
        return
    with zipfile.ZipFile(path) as z:
        z.extractall(dest_dir)  # extractall strips ../ and absolute paths
    path.unlink()


def load_state(out):
    try:
        return json.loads((out / STATE_FILE).read_text())
    except (OSError, ValueError):
        return {}


def save_state(out, state):
    tmp = out / (STATE_FILE + ".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    tmp.replace(out / STATE_FILE)


def process(s, args, item, url, log=_stderr):
    artist = safe_name(item.get("band_name"))
    title = safe_name(item.get("item_title") or item.get("album_title"))
    dest = args.output / artist / title
    blob = page_blob(s, url)
    fmt, file_url = pick_download(blob, args.format)
    path = download_file(s, file_url, dest, f"{title}.{fmt}", args.retries,
                         log)
    if not args.keep_zip:
        unpack(path, dest)
    return fmt, dest


def build_todo(items, state, args, log=print):
    """Split the collection into what still needs downloading."""
    todo, skipped = [], 0
    for item, url in items:
        key = f"{item.get('sale_item_type')}{item.get('sale_item_id')}"
        label = f"{item.get('band_name')} - {item.get('item_title')}"
        if not url:
            log(f"  no download (preorder/subscription?): {label}")
            skipped += 1
        elif not args.force and state.get(key, {}).get("format") in args.format:
            skipped += 1
        else:
            todo.append((key, label, item, url))
    if args.limit:
        todo = todo[:args.limit]
    return todo, skipped


def download_all(s, args, todo, state, on_done, log=_stderr, stop=None):
    """Download todo with args.jobs workers; return the failed labels.

    on_done(n, label, fmt, error) is called once per item as it finishes.
    If `stop` (a threading.Event) gets set, queued items are dropped and
    the ones already running are allowed to finish.
    """
    def run(item, url):
        if stop is not None and stop.is_set():
            return None  # stopped before this one began
        return process(s, args, item, url, log)

    failed = []
    pool = ThreadPoolExecutor(max_workers=max(1, args.jobs))
    try:
        futures = {pool.submit(run, item, url): (key, label)
                   for key, label, item, url in todo}
        n = 0
        for fut in as_completed(futures):
            key, label = futures[fut]
            if fut.exception() is None and fut.result() is None:
                continue  # skipped by stop
            n += 1
            try:
                fmt, dest = fut.result()
            except Exception as e:  # keep going; report at the end
                failed.append(label)
                on_done(n, label, None, e)
            else:
                state[key] = {"format": fmt, "path": str(dest),
                              "title": label, "at": int(time.time())}
                save_state(args.output, state)
                on_done(n, label, fmt, None)
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    return failed


def main():
    p = argparse.ArgumentParser(
        description="Download your Bandcamp collection in a chosen format.")
    p.add_argument("-f", "--format", type=resolve_formats, default="flac",
                   help="format, or comma list in order of preference "
                        "(e.g. 'flac,wav'). One of: " + ", ".join(FORMATS) +
                        "; aliases: mp3 ogg aac m4a aiff v0. Default: flac")
    p.add_argument("-o", "--output", type=Path, default=Path("bandcamp"),
                   help="output directory (default: ./bandcamp)")
    p.add_argument("-c", "--cookies", help="Netscape cookies.txt exported "
                   "from a browser logged in to bandcamp.com")
    p.add_argument("-i", "--identity", help="value of the bandcamp.com "
                   "'identity' cookie (or set BANDCAMP_IDENTITY)")
    p.add_argument("-j", "--jobs", type=int, default=3,
                   help="parallel downloads (default 3; be polite)")
    p.add_argument("--retries", type=int, default=3)
    p.add_argument("--include-hidden", action="store_true",
                   help="also download items hidden from your profile")
    p.add_argument("--keep-zip", action="store_true",
                   help="keep album zips instead of extracting them")
    p.add_argument("--force", action="store_true",
                   help="re-download items already done in this format")
    p.add_argument("--limit", type=int, help="stop after N items (testing)")
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="list what would be downloaded, download nothing")
    args = p.parse_args()
    if isinstance(args.format, str):  # argparse skips type= for defaults
        args.format = resolve_formats(args.format)

    try:
        s = make_session(args)
        fan_id, user = get_fan_id(s)
    except (BandcampError, OSError, requests.RequestException) as e:
        sys.exit(f"error: {e}")
    print(f"Logged in as {user} (fan {fan_id}); "
          f"format preference: {', '.join(args.format)}")

    items = list(iter_collection(s, fan_id))
    if args.include_hidden:
        items += list(iter_collection(s, fan_id, "hidden_items"))
    print(f"{len(items)} items in collection")

    args.output.mkdir(parents=True, exist_ok=True)
    state = load_state(args.output)
    todo, skipped = build_todo(items, state, args)
    print(f"{len(todo)} to download, {skipped} skipped")

    if args.dry_run:
        for _, label, _, _ in todo:
            print(f"  {label}")
        return

    def report(n, label, fmt, error):
        if error:
            print(f"[{n}/{len(todo)}] FAILED {label}: {error}")
        else:
            print(f"[{n}/{len(todo)}] {fmt:<13} {label}")

    failed = download_all(s, args, todo, state, report)

    print(f"done: {len(todo) - len(failed)} downloaded, {len(failed)} failed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
