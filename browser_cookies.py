"""Read the bandcamp.com `identity` cookie straight out of a browser.

Safari: ~/Library/.../Cookies.binarycookies, a small binary format that is
not encrypted, but macOS only lets apps with Full Disk Access read it.

Chrome family (Chrome, Chromium, Brave, Edge): an SQLite file whose values
are AES-128-CBC encrypted. The key is PBKDF2(password, "saltysalt"):
  macOS   password in the login keychain ("Chrome Safe Storage"); macOS asks
          once whether `security` may read it
  Linux   v11 = password from the desktop keyring, v10 = "peanuts"
Decryption uses the `openssl` command (shipped with macOS and most Linux),
so no extra Python packages are needed. Windows Chrome (app-bound
encryption since Chrome 127) is not supported; paste the cookie instead.
"""

import hashlib
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

HOME = Path.home()
MAC = sys.platform == "darwin"

# name: (macOS data dir, Linux data dir, keychain service/account, Linux
#        keyring application attribute)
CHROMIUMS = {
    "chrome": ("Google/Chrome", "google-chrome", "Chrome", "chrome"),
    "chromium": ("Chromium", "chromium", "Chromium", "chromium"),
    "brave": ("BraveSoftware/Brave-Browser", "BraveSoftware/Brave-Browser",
              "Brave", "brave"),
    "edge": ("Microsoft Edge", "microsoft-edge", "Microsoft Edge",
             "microsoft-edge"),
}
BROWSERS = ["safari"] + list(CHROMIUMS)

SAFARI_FILES = [
    HOME / "Library/Containers/com.apple.Safari/Data/Library/Cookies"
           "/Cookies.binarycookies",
    HOME / "Library/Cookies/Cookies.binarycookies",  # Safari before 14
]


class BrowserCookieError(Exception):
    pass


def get_identity(browser, profile=None, domain="bandcamp.com",
                 name="identity"):
    """Return the cookie value, or raise BrowserCookieError saying why not."""
    browser = browser.lower()
    if browser == "safari":
        return _safari(domain, name)
    if browser in CHROMIUMS:
        return _chromium(browser, profile, domain, name)
    raise BrowserCookieError(
        f"unknown browser {browser!r}; choose from {', '.join(BROWSERS)}")


# ----------------------------------------------------------------- Safari

def _safari(domain, name):
    if not MAC:
        raise BrowserCookieError("Safari cookies can only be read on macOS")
    for path in SAFARI_FILES:
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            continue
        except PermissionError:
            raise BrowserCookieError(
                "macOS blocked reading Safari's cookies: give your terminal "
                "app Full Disk Access (System Settings > Privacy & Security "
                "> Full Disk Access), then open a new terminal window")
        for host, cname, value in parse_binarycookies(data):
            if cname == name and host.lstrip(".").endswith(domain):
                return value
    raise BrowserCookieError(
        f"no {domain} '{name}' cookie in Safari; log in to {domain} in "
        "Safari first")


def parse_binarycookies(data):
    """Yield (domain, name, value) from a Cookies.binarycookies blob."""
    if data[:4] != b"cook":
        raise BrowserCookieError("not a Safari cookies file")
    (pages,) = struct.unpack(">I", data[4:8])
    sizes = struct.unpack(f">{pages}I", data[8:8 + 4 * pages])
    pos = 8 + 4 * pages
    for size in sizes:
        page = data[pos:pos + size]
        pos += size
        (count,) = struct.unpack("<I", page[4:8])
        for off in struct.unpack(f"<{count}I", page[8:8 + 4 * count]):
            (length,) = struct.unpack("<I", page[off:off + 4])
            rec = page[off:off + length]
            dom, nam, _path, val = struct.unpack("<4I", rec[16:32])
            yield _cstr(rec, dom), _cstr(rec, nam), _cstr(rec, val)


def _cstr(buf, start):
    return buf[start:buf.index(b"\0", start)].decode("utf-8", "replace")


# ---------------------------------------------------------- Chrome family

def _chromium(browser, profile, domain, name):
    mac_dir, linux_dir, keychain, keyring_app = CHROMIUMS[browser]
    if MAC:
        root = HOME / "Library/Application Support" / mac_dir
    elif sys.platform.startswith("linux"):
        root = HOME / ".config" / linux_dir
    else:
        raise BrowserCookieError(
            f"reading {browser} cookies is only supported on macOS and "
            "Linux; paste the identity cookie instead")
    dbs = _cookie_dbs(root, profile)
    if not dbs:
        raise BrowserCookieError(
            f"no {browser} profile found in {root}"
            + (f" named {profile!r}" if profile else ""))

    rows = []  # (last_access, host, encrypted, plain, db_version)
    for db in dbs:
        rows += _read_rows(db, domain, name)
    if not rows:
        raise BrowserCookieError(
            f"no {domain} '{name}' cookie in {browser}; log in to {domain} "
            f"in {browser} first")
    rows.sort(reverse=True)  # most recently used profile first

    keys = None
    for _access, host, enc, plain, version in rows:
        if plain:
            return plain
        if keys is None:
            keys = _keys(keychain, keyring_app)
        value = _decrypt(enc, keys, host, version)
        if value:
            return value
    raise BrowserCookieError(
        f"could not decrypt the {browser} cookie (keyring password wrong or "
        "unavailable); paste the identity cookie instead")


def _cookie_dbs(root, profile):
    names = [profile] if profile else (
        ["Default"] + sorted(p.name for p in root.glob("Profile *")))
    out = []
    for n in names:
        for rel in ("Network/Cookies", "Cookies"):
            if (root / n / rel).is_file():
                out.append(root / n / rel)
                break
    return out


def _read_rows(db, domain, name):
    # Copy first: the browser keeps the file locked and may hold recent
    # writes in the -wal file next to it.
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "Cookies"
        shutil.copy(db, copy)
        for extra in ("-wal", "-shm"):
            side = db.with_name(db.name + extra)
            if side.exists():
                shutil.copy(side, copy.with_name(copy.name + extra))
        con = sqlite3.connect(copy)
        try:
            try:
                (version,) = con.execute(
                    "SELECT value FROM meta WHERE key='version'").fetchone()
                version = int(version)
            except (sqlite3.Error, TypeError, ValueError):
                version = 0
            cur = con.execute(
                "SELECT last_access_utc, host_key, encrypted_value, value "
                "FROM cookies WHERE name = ? AND "
                "(host_key = ? OR host_key LIKE ?)",
                (name, domain, "%." + domain))
            return [(a, h, bytes(e or b""), v, version)
                    for a, h, e, v in cur.fetchall()]
        except sqlite3.Error as e:
            raise BrowserCookieError(f"cannot read {db}: {e}")
        finally:
            con.close()


def _pbkdf2(password, rounds):
    return hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", rounds, 16)


def _keys(keychain, keyring_app):
    """Candidate keys by prefix: {b'v10': [...], b'v11': [...]}."""
    if MAC:
        try:
            pw = subprocess.run(
                ["security", "find-generic-password", "-w",
                 "-a", keychain, "-s", f"{keychain} Safe Storage"],
                capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise BrowserCookieError(f"keychain lookup failed: {e}")
        if pw.returncode != 0:
            raise BrowserCookieError(
                f"macOS keychain refused '{keychain} Safe Storage' (click "
                "Allow when macOS asks): " + pw.stderr.strip())
        return {b"v10": [_pbkdf2(pw.stdout.strip().encode(), 1003)]}

    v11 = []
    try:
        pw = subprocess.run(["secret-tool", "lookup", "application",
                             keyring_app], capture_output=True, timeout=30)
        if pw.returncode == 0 and pw.stdout.strip():
            v11.append(_pbkdf2(pw.stdout.strip(), 1))
    except (OSError, subprocess.TimeoutExpired):
        pass  # no libsecret tools; the empty password still sometimes works
    v11.append(_pbkdf2(b"", 1))
    return {b"v10": [_pbkdf2(b"peanuts", 1)], b"v11": v11}


def _decrypt(enc, keys, host, version):
    prefix, body = enc[:3], enc[3:]
    for key in keys.get(prefix, []):
        plain = aes_cbc_decrypt(key, body)
        if plain is None:
            continue
        # Chrome 130+ (db version 24) prefixes SHA-256 of the host.
        if version >= 24 and plain[:32] == hashlib.sha256(
                host.encode()).digest():
            plain = plain[32:]
        try:
            return plain.decode("utf-8")
        except UnicodeDecodeError:
            continue  # wrong key that happened to give valid padding
    return None


def aes_cbc_decrypt(key, data, iv=b" " * 16):
    """AES-128-CBC + PKCS#7 via the openssl CLI; None if the key is wrong."""
    if not shutil.which("openssl"):
        raise BrowserCookieError("the `openssl` command is needed to decrypt "
                                 "Chrome cookies")
    r = subprocess.run(
        ["openssl", "enc", "-d", "-aes-128-cbc", "-K", key.hex(),
         "-iv", iv.hex()], input=data, capture_output=True, timeout=30)
    return r.stdout if r.returncode == 0 else None
