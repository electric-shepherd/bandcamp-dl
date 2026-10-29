"""Offline tests for browser_cookies: fake Safari and Chrome cookie stores
built to the real on-disk formats, decrypted end to end.

    python3 -m unittest discover -s tests
"""

import hashlib
import os
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bandcamp_dl  # noqa: E402
import browser_cookies as bcook  # noqa: E402

VALUE = "7%09AbCdEf%2Bghij%3D%7B%22id%22%3A1%7D"


def binarycookies(cookies):
    """Build a one-page Cookies.binarycookies file."""
    recs = []
    for domain, name, value in cookies:
        strings = b""
        offs = []
        for text in (domain, name, "/", value):
            offs.append(56 + len(strings))
            strings += text.encode() + b"\0"
        rec = struct.pack("<4I", 56 + len(strings), 0, 0, 0)
        rec += struct.pack("<4I", *offs) + b"\0" * 8
        rec += struct.pack("<2d", 8e8, 7e8) + strings
        recs.append(rec)
    head = 8 + 4 * len(recs) + 4
    offsets, pos = [], head
    for r in recs:
        offsets.append(pos)
        pos += len(r)
    page = (b"\0\0\1\0" + struct.pack("<I", len(recs))
            + struct.pack(f"<{len(recs)}I", *offsets) + b"\0" * 4
            + b"".join(recs))
    return b"cook" + struct.pack(">I", 1) + struct.pack(">I", len(page)) + page


def encrypt(key, plain):
    r = subprocess.run(["openssl", "enc", "-e", "-aes-128-cbc", "-K",
                        key.hex(), "-iv", (b" " * 16).hex()],
                       input=plain, capture_output=True, check=True)
    return r.stdout


def chrome_db(path, rows, version):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE meta (key TEXT, value TEXT)")
    con.execute("INSERT INTO meta VALUES ('version', ?)", (str(version),))
    con.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT,"
                " encrypted_value BLOB, last_access_utc INTEGER)")
    con.executemany("INSERT INTO cookies VALUES (?,?,?,?,?)", rows)
    con.commit()
    con.close()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        for name, val in (("HOME", self.home),
                          ("SAFARI_FILES", [self.home / "Cookies.bc"])):
            p = mock.patch.object(bcook, name, val)
            p.start()
            self.addCleanup(p.stop)

    def platform(self, mac):
        p1 = mock.patch.object(bcook, "MAC", mac)
        p2 = mock.patch.object(bcook.sys, "platform",
                               "darwin" if mac else "linux")
        for p in (p1, p2):
            p.start()
            self.addCleanup(p.stop)


class SafariTest(Base):
    def test_reads_identity(self):
        self.platform(mac=True)
        (self.home / "Cookies.bc").write_bytes(binarycookies([
            (".example.com", "identity", "nope"),
            (".bandcamp.com", "session", "x"),
            (".bandcamp.com", "identity", VALUE),
        ]))
        self.assertEqual(bcook.get_identity("safari"), VALUE)

    def test_missing_cookie(self):
        self.platform(mac=True)
        (self.home / "Cookies.bc").write_bytes(
            binarycookies([(".bandcamp.com", "session", "x")]))
        with self.assertRaisesRegex(bcook.BrowserCookieError, "log in"):
            bcook.get_identity("safari")

    def test_permission_hint(self):
        self.platform(mac=True)
        f = self.home / "Cookies.bc"
        f.write_bytes(b"cook")
        f.chmod(0)
        if os.access(f, os.R_OK):
            self.skipTest("running as root")
        with self.assertRaisesRegex(bcook.BrowserCookieError,
                                    "Full Disk Access"):
            bcook.get_identity("safari")


class ChromeTest(Base):
    def linux_db(self, rows, version=24, profile="Default"):
        chrome_db(self.home / ".config/google-chrome" / profile / "Cookies",
                  rows, version)

    def test_linux_v10_peanuts_v24_hash_prefix(self):
        self.platform(mac=False)
        host = ".bandcamp.com"
        plain = hashlib.sha256(host.encode()).digest() + VALUE.encode()
        enc = b"v10" + encrypt(bcook._pbkdf2(b"peanuts", 1), plain)
        self.linux_db([(host, "identity", "", enc, 5)])
        self.assertEqual(bcook.get_identity("chrome"), VALUE)

    def test_linux_v11_empty_password_old_db(self):
        self.platform(mac=False)
        enc = b"v11" + encrypt(bcook._pbkdf2(b"", 1), VALUE.encode())
        self.linux_db([(".bandcamp.com", "identity", "", enc, 5)],
                      version=18)
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent:"
                                          + os.environ["PATH"]}):
            self.assertEqual(bcook.get_identity("chrome"), VALUE)

    def test_newest_profile_wins(self):
        self.platform(mac=False)
        key = bcook._pbkdf2(b"peanuts", 1)
        self.linux_db([(".bandcamp.com", "identity", "",
                        b"v10" + encrypt(key, b"old"), 1)], version=18)
        self.linux_db([(".bandcamp.com", "identity", "",
                        b"v10" + encrypt(key, VALUE.encode()), 9)],
                      version=18, profile="Profile 2")
        self.assertEqual(bcook.get_identity("chrome"), VALUE)
        self.assertEqual(bcook.get_identity("chrome", "Default"), "old")

    def test_mac_keychain(self):
        self.platform(mac=True)
        bindir = self.home / "bin"
        bindir.mkdir()
        fake = bindir / "security"
        fake.write_text("#!/bin/sh\necho 'hunter2'\n")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        host = "bandcamp.com"
        plain = hashlib.sha256(host.encode()).digest() + VALUE.encode()
        enc = b"v10" + encrypt(bcook._pbkdf2(b"hunter2", 1003), plain)
        chrome_db(self.home / "Library/Application Support/Google/Chrome"
                  "/Default/Cookies", [(host, "identity", "", enc, 3)], 24)
        with mock.patch.dict(os.environ, {"PATH": f"{bindir}:"
                                          + os.environ["PATH"]}):
            self.assertEqual(bcook.get_identity("chrome"), VALUE)

    def test_wrong_key_reports(self):
        self.platform(mac=False)
        enc = b"v11" + encrypt(bcook._pbkdf2(b"secret", 1), VALUE.encode())
        self.linux_db([(".bandcamp.com", "identity", "", enc, 5)])
        real_run = subprocess.run

        def fake(cmd, *a, **k):
            if cmd[0] == "secret-tool":
                raise FileNotFoundError
            return real_run(cmd, *a, **k)
        with mock.patch.object(bcook.subprocess, "run", side_effect=fake):
            with self.assertRaisesRegex(bcook.BrowserCookieError,
                                        "could not decrypt"):
                bcook.get_identity("chrome")

    def test_no_profile(self):
        self.platform(mac=False)
        with self.assertRaisesRegex(bcook.BrowserCookieError, "no chrome"):
            bcook.get_identity("chrome")


class CliTest(Base):
    def test_saved_login_used_and_private(self):
        cfg = self.home / "cfg"
        with mock.patch.object(bandcamp_dl, "CONFIG_DIR", cfg), \
                mock.patch.object(bandcamp_dl, "IDENTITY_FILE",
                                  cfg / "identity"), \
                mock.patch.dict(os.environ, {}, clear=False) as env:
            env.pop("BANDCAMP_IDENTITY", None)
            bandcamp_dl.save_identity("identity=" + VALUE)
            self.assertEqual(oct((cfg / "identity").stat().st_mode & 0o777),
                             "0o600")
            args = mock.Mock(identity=None, from_browser=None, cookies=None)
            s = bandcamp_dl.make_session(args)
            self.assertEqual(s.cookies.get("identity"), VALUE)
            bandcamp_dl.save_identity(None)
            with self.assertRaisesRegex(bandcamp_dl.BandcampError, "--login"):
                bandcamp_dl.make_session(args)


if __name__ == "__main__":
    unittest.main()
