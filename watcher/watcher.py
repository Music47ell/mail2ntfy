#!/usr/bin/env python3
"""mail2ntfy: monitor IMAP mailboxes and publish new-mail notifications to ntfy.

Uses only the Python standard library. Mailboxes are watched with IMAP IDLE
(imaplib's native Idler, Python 3.14+) for near-instant notifications, with a
polling loop as a safety net and as a fallback for servers that do not
advertise IDLE. State is kept in a SQLite database so the same email is never
notified twice, even across restarts.
"""

import email
import imaplib
import json
import logging
import os
import random
import re
import signal
import sqlite3
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from email.header import decode_header
from email.utils import parseaddr

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("mail2ntfy")

DB_PATH = os.environ.get("DB_PATH", "/data/notified.db")
BASE_DELAY = float(os.environ.get("RECONNECT_BASE_DELAY", "5"))
MAX_DELAY = float(os.environ.get("RECONNECT_MAX_DELAY", "300"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))
# Keep IDLE renewals within RFC 2177's 29-minute guideline and above a floor so
# a misconfiguration cannot cause a busy IDLE loop.
IDLE_TIMEOUT = max(5, min(int(os.environ.get("IDLE_TIMEOUT", "300")), 1740))

GMAIL_HOST = "imap.gmail.com"
GMAIL_PORT = 993
IMAP_HOST = "imap.example.com"
IMAP_PORT = 993


# ---------------------------------------------------------------------------
# SQLite state
# ---------------------------------------------------------------------------

_db = None
_db_lock = threading.Lock()


def init_db():
    """Create/open the SQLite database and its tables. Must be called once."""
    global _db
    with _db_lock:
        directory = os.path.dirname(DB_PATH)
        if directory:
            os.makedirs(directory, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS seen ("
            " account TEXT NOT NULL,"
            " uidvalidity INTEGER NOT NULL,"
            " uid INTEGER NOT NULL,"
            " PRIMARY KEY (account, uidvalidity, uid));"
            "CREATE TABLE IF NOT EXISTS state ("
            " account TEXT PRIMARY KEY,"
            " uidvalidity INTEGER NOT NULL,"
            " last_uid INTEGER NOT NULL);"
        )
        conn.commit()
        _db = conn


def load_state(account):
    with _db_lock:
        row = _db.execute(
            "SELECT uidvalidity, last_uid FROM state WHERE account = ?",
            (account,),
        ).fetchone()
    if row is None:
        return None
    return int(row["uidvalidity"]), int(row["last_uid"])


def save_state(account, uidvalidity, last_uid):
    with _db_lock:
        _db.execute(
            "INSERT INTO state (account, uidvalidity, last_uid) VALUES (?, ?, ?) "
            "ON CONFLICT(account) DO UPDATE SET "
            "uidvalidity = excluded.uidvalidity, last_uid = excluded.last_uid",
            (account, uidvalidity, last_uid),
        )
        _db.commit()


def clear_seen(account):
    with _db_lock:
        _db.execute("DELETE FROM seen WHERE account = ?", (account,))
        _db.commit()


def is_seen(account, uidvalidity, uid):
    with _db_lock:
        row = _db.execute(
            "SELECT 1 FROM seen WHERE account = ? AND uidvalidity = ? AND uid = ?",
            (account, uidvalidity, uid),
        ).fetchone()
    return row is not None


def mark_seen(account, uidvalidity, uid):
    with _db_lock:
        _db.execute(
            "INSERT OR IGNORE INTO seen (account, uidvalidity, uid) VALUES (?, ?, ?)",
            (account, uidvalidity, uid),
        )
        _db.commit()


# ---------------------------------------------------------------------------
# Header decoding and ntfy delivery
# ---------------------------------------------------------------------------


def _decode_mime(value):
    """Decode RFC 2047 encoded words / charset bytes into a plain string."""
    if not value:
        return ""
    try:
        parts = []
        for raw, charset in decode_header(value):
            if isinstance(raw, bytes):
                parts.append(raw.decode(charset or "utf-8", errors="replace"))
            else:
                parts.append(raw)
        text = "".join(parts)
    except Exception:
        text = str(value)
    return re.sub(r"[\r\n\t]+", " ", text).strip()


def _format_address(raw_header):
    """Return a display string for an address header, e.g. 'Name <addr>'."""
    name, addr = parseaddr(raw_header or "")
    name = _decode_mime(name)
    if name and addr:
        return f"{name} <{addr}>"
    if addr:
        return addr
    return _decode_mime(raw_header) or "unknown"


def send_ntfy(title, message):
    """POST a JSON notification to the configured ntfy topic."""
    base = os.environ.get("NTFY_URL", "http://ntfy:80").rstrip("/")
    topic = os.environ.get("NTFY_TOPIC", "email").strip("/")
    token = os.environ.get("NTFY_TOKEN", "").strip()
    url = f"{base}/"
    body = json.dumps(
        {
            "topic": topic,
            "title": title,
            "message": message,
            "tags": ["email"],
        }
    ).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "mail2ntfy/1.0")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            code = resp.status
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise RuntimeError(
                "ntfy rejected credentials (HTTP %d) - check NTFY_TOKEN and "
                "topic publish permissions" % exc.code
            ) from exc
        raise RuntimeError("ntfy returned HTTP %d" % exc.code) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("cannot reach ntfy at %s: %s" % (url, exc.reason)) from exc
    if not 200 <= code < 300:
        raise RuntimeError("ntfy returned HTTP %d" % code)


# ---------------------------------------------------------------------------
# Account monitoring
# ---------------------------------------------------------------------------


class Account:
    def __init__(self, key, label, host, port, user, password):
        self.key = key
        self.label = label
        self.host = host
        self.port = port
        self.user = user
        self.password = password

    # -- logging helpers (never log credentials) --------------------------

    def _info(self, fmt, *args):
        log.info("[%s] " + fmt, self.label, *args)

    def _warn(self, fmt, *args):
        log.warning("[%s] " + fmt, self.label, *args)

    def _error(self, fmt, *args):
        log.error("[%s] " + fmt, self.label, *args)

    @staticmethod
    def _err(exc):
        text = str(exc).strip() or type(exc).__name__
        return text[:300]

    # -- connection management --------------------------------------------

    def _connect(self):
        ctx = ssl.create_default_context()
        conn = imaplib.IMAP4_SSL(self.host, self.port, ssl_context=ctx)
        conn.sock.settimeout(60)
        try:
            conn.login(self.user, self.password)
            typ, _data = conn.select("INBOX")
            if typ != "OK":
                raise RuntimeError("SELECT INBOX failed")
        except Exception:
            try:
                conn.logout()
            except Exception:
                pass
            raise
        return conn

    def _uidvalidity(self, conn):
        typ, data = conn.status("INBOX", "(UIDVALIDITY)")
        if typ != "OK" or not data or not data[0]:
            raise RuntimeError("could not read UIDVALIDITY")
        match = re.search(rb"UIDVALIDITY\s+(\d+)", data[0])
        if not match:
            raise RuntimeError("UIDVALIDITY missing from server response")
        return int(match.group(1))

    def _current_max_uid(self, conn):
        typ, data = conn.uid("SEARCH", None, "ALL")
        if typ != "OK":
            raise RuntimeError("UID SEARCH ALL failed")
        if data and data[0]:
            uids = [int(x) for x in data[0].split()]
            return max(uids)
        return 0

    def _reset_baseline(self, conn, uidvalidity):
        """(Re)initialize state: never notify for mail that already exists."""
        highest = self._current_max_uid(conn)
        clear_seen(self.key)
        save_state(self.key, uidvalidity, highest)
        self._info(
            "startup baseline set (UIDVALIDITY %d, highest UID %d): "
            "only mail arriving after this point will be notified",
            uidvalidity,
            highest,
        )

    def _fetch_header(self, conn, uid):
        """Return (sender, recipient, subject) for one message. Returns None
        if the message vanished between the search and this fetch."""
        typ, data = conn.uid("FETCH", str(uid), "(BODY.PEEK[HEADER])")
        if typ != "OK":
            raise RuntimeError("UID FETCH failed")
        raw = None
        if data:
            for part in data:
                if isinstance(part, tuple) and len(part) >= 2:
                    raw = part[1]
                    break
        if raw is None:
            return None
        msg = email.message_from_bytes(raw)
        subject = _decode_mime(msg.get("Subject", "")) or "(no subject)"
        sender = _format_address(msg.get("From", ""))
        recipient = _format_address(msg.get("To", ""))
        return sender, recipient, subject

    # -- polling -----------------------------------------------------------

    def _poll_once(self, conn, uidvalidity):
        state = load_state(self.key)
        if state is None or state[0] != uidvalidity:
            self._reset_baseline(conn, uidvalidity)
            return

        last_uid = state[1]
        start = last_uid + 1
        typ, data = conn.uid("SEARCH", None, "UID", f"{start}:*")
        if typ != "OK":
            raise RuntimeError("UID SEARCH failed")
        uids = []
        if data and data[0]:
            uids = sorted(int(x) for x in data[0].split())

        candidate = last_uid
        can_advance = True
        changed = False

        for uid in uids:
            if uid <= candidate:
                continue
            if is_seen(self.key, uidvalidity, uid):
                if can_advance:
                    candidate = uid
                    changed = True
                continue

            header = self._fetch_header(conn, uid)
            if header is None:
                # Message was expunged between search and fetch; treat as handled.
                mark_seen(self.key, uidvalidity, uid)
                if can_advance:
                    candidate = uid
                    changed = True
                continue

            sender, recipient, subject = header
            self._info("new email from %s: %s", sender, subject)
            try:
                send_ntfy("You've Got Mail", f"From: {sender}\nTo: {recipient}")
            except Exception as exc:
                # Leave the UID unmarked and stop advancing past it; it will be
                # retried on a later poll. Later mail is still delivered.
                self._error("notification send failed, will retry: %s", self._err(exc))
                can_advance = False
                continue

            mark_seen(self.key, uidvalidity, uid)
            if can_advance:
                candidate = uid
            changed = True
            self._info("notification sent")

        if changed and candidate != last_uid:
            save_state(self.key, uidvalidity, candidate)

    # -- main loop ----------------------------------------------------------

    def _delay(self, attempt):
        """Exponential backoff with jitter, capped at MAX_DELAY."""
        if attempt <= 0:
            return 0.0
        delay = min(BASE_DELAY * (2 ** (attempt - 1)), MAX_DELAY)
        return delay * (0.5 + random.random())

    def run(self, stop):
        failures = 0
        while not stop.is_set():
            conn = None
            try:
                conn = self._connect()
            except Exception as exc:
                self._error("connect failed: %s", self._err(exc))
                failures += 1
                if stop.wait(self._delay(failures)):
                    return
                continue

            self._info("connected")
            failures = 0
            try:
                self._session(conn, stop)
            except Exception as exc:
                self._warn("connection lost: %s", self._err(exc))
                failures += 1
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
            self._info("disconnected")

            if stop.is_set():
                return
            if stop.wait(self._delay(failures)):
                return

    def _poll_loop(self, conn, uidvalidity, stop):
        """Fallback used when the server does not support IDLE."""
        while not stop.is_set():
            self._poll_once(conn, uidvalidity)
            if stop.wait(POLL_INTERVAL):
                return

    def _session(self, conn, stop):
        uidvalidity = self._uidvalidity(conn)
        state = load_state(self.key)
        if state is None or state[0] != uidvalidity:
            self._reset_baseline(conn, uidvalidity)

        if "IDLE" not in conn.capabilities:
            self._warn(
                "server does not advertise IDLE; polling every %ds", POLL_INTERVAL
            )
            return self._poll_loop(conn, uidvalidity, stop)

        # Catch up on mail that arrived while we were disconnected, then IDLE.
        self._poll_once(conn, uidvalidity)
        self._info("watching INBOX with IDLE (renew every %ds)", IDLE_TIMEOUT)

        while not stop.is_set():
            try:
                with conn.idle(duration=IDLE_TIMEOUT) as idler:
                    for typ, _data in idler:
                        if typ == "EXISTS":
                            self._info("server announced new mail (IDLE)")
                            break
            except imaplib.IMAP4.error as exc:
                if isinstance(exc, imaplib.IMAP4.abort):
                    # Connection broke; let run() reconnect and retry.
                    raise
                self._warn(
                    "IDLE unavailable (%s); polling every %ds",
                    self._err(exc),
                    POLL_INTERVAL,
                )
                return self._poll_loop(conn, uidvalidity, stop)

            if stop.is_set():
                return
            # Runs after an EXISTS *and* after an IDLE window expires, so no
            # mail is ever missed even if an EXISTS notification is dropped.
            self._poll_once(conn, uidvalidity)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def load_accounts():
    """Discover configured accounts from environment variables."""
    accounts = []
    gmail_user = os.environ.get("GMAIL_USER", "").strip()
    gmail_pass = os.environ.get("GMAIL_PASSWORD", "")
    if gmail_user or gmail_pass:
        if gmail_user and gmail_pass:
            accounts.append(
                Account("gmail", "Gmail", GMAIL_HOST, GMAIL_PORT, gmail_user, gmail_pass)
            )
        else:
            log.warning("GMAIL_USER and GMAIL_PASSWORD must both be set; ignoring Gmail")

    imap_user = os.environ.get("IMAP_USER", "").strip()
    imap_pass = os.environ.get("IMAP_PASSWORD", "")
    imap_name = os.environ.get("IMAP_NAME", "").strip()
    if imap_user or imap_pass:
        if imap_user and imap_pass:
            label = imap_name or "IMAP"
            accounts.append(
                Account("imap", label, IMAP_HOST, IMAP_PORT, imap_user, imap_pass)
            )
        else:
            log.warning("IMAP_USER and IMAP_PASSWORD must both be set; ignoring IMAP")

    return accounts


def main():
    log.info("mail2ntfy starting")
    try:
        init_db()
    except Exception as exc:
        log.critical("cannot open database %s: %s", DB_PATH, exc)
        log.critical(
            "check that the host directory is writable by UID 1000, e.g. "
            "sudo chown -R 1000:1000 /opt/docker/data/mail2ntfy"
        )
        sys.exit(1)

    accounts = load_accounts()
    if not accounts:
        log.error(
            "no mailboxes configured: set GMAIL_USER/GMAIL_PASSWORD and/or "
            "IMAP_NAME/USER/PASSWORD in the environment"
        )
        sys.exit(1)

    log.info("configured mailboxes: %s", ", ".join(a.label for a in accounts))

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    threads = []
    for account in accounts:
        thread = threading.Thread(
            target=account.run, args=(stop,), name=account.key, daemon=True
        )
        thread.start()
        threads.append(thread)

    while not stop.is_set():
        stop.wait(1)

    log.info("shutting down")
    for thread in threads:
        thread.join(timeout=10)


if __name__ == "__main__":
    main()
