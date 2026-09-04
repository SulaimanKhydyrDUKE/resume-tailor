"""One-time codes from the applicant's inbox.

Oracle HCM (and a few others) send a verification code by e-mail before
they show the application form. A person opens the mail and types it; this
does the same over IMAP with an app password, and nothing else — it reads
recent messages that look like a code, never sends, never deletes.

Configure in ~/.resume-tailor/env:
    RESUME_TAILOR_IMAP_USER=you@gmail.com        (defaults to the profile e-mail)
    RESUME_TAILOR_IMAP_PASSWORD=xxxx xxxx xxxx xxxx  (a Google app password)
    RESUME_TAILOR_IMAP_HOST=imap.gmail.com         (default)
"""
from __future__ import annotations

import asyncio
import email
import imaplib
import os
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

CODE_RE = re.compile(
    r"(?:code|passcode|pass code|pin|otp|one-time password|verification)[^0-9]{0,60}?\b(\d{4,8})\b", re.I)
LOOKS_LIKE_CODE_MAIL = re.compile(r"verif|confirm your identity|one-time|passcode|pass code|security code|access code|sign.?in link|magic link|continue your application|complete your application", re.I)
LINK_RE = re.compile(r"https?://[^\s\"'<>)\]]{12,400}", re.I)
_LINK_SKIP = re.compile(r"unsubscribe|privacy|terms|facebook|twitter|linkedin\.com/company|instagram|youtube|apple\.com|play\.google|\.png|\.jpg|\.gif|logo", re.I)


def configured() -> bool:
    from .llm import _load_env_file

    _load_env_file()
    return bool(os.environ.get("RESUME_TAILOR_IMAP_PASSWORD"))


def extract_link(text: str, html: str = "") -> str | None:
    """The sign-in or continue link a message carries (the longest candidate
    that is not a footer link), or None."""
    cands = [u.rstrip(".,;") for u in LINK_RE.findall((html or "") + " " + (text or "")) if not _LINK_SKIP.search(u)]
    cands = [u for u in cands if re.search(r"login|signin|sign-in|verify|token|confirm|continue|apply|candidate|auth|magic|link", u, re.I)]
    return max(cands, key=len) if cands else None


def extract_code(text: str) -> str | None:
    """The code a message carries, or None."""
    m = CODE_RE.search(text or "")
    return m.group(1) if m else None


def _profile_email() -> str:
    from .profile import Profile

    return str(Profile.load().career.get("personal_information", {}).get("email") or "")


def _body_html(msg) -> str:
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        if part.get_content_type() == "text/html":
            try:
                return (part.get_payload(decode=True) or b"").decode(part.get_content_charset() or "utf-8", errors="replace")
            except Exception:
                return ""
    return ""


def _body_text(msg) -> str:
    parts = []
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        if ctype == "text/html":
            text = re.sub(r"<[^>]+>", " ", text)
        parts.append(text)
    return re.sub(r"\s+", " ", " ".join(parts))


def _search_once(since: float, hints: list[str]) -> str | None:
    host = os.environ.get("RESUME_TAILOR_IMAP_HOST", "imap.gmail.com")
    user = os.environ.get("RESUME_TAILOR_IMAP_USER") or _profile_email()
    password = os.environ["RESUME_TAILOR_IMAP_PASSWORD"].replace(" ", "")
    floor = datetime.fromtimestamp(since, tz=timezone.utc) - timedelta(seconds=90)
    box = imaplib.IMAP4_SSL(host)
    try:
        box.login(user, password)
        box.select("INBOX", readonly=True)
        _, data = box.uid("search", None, "SINCE", (floor - timedelta(days=1)).strftime("%d-%b-%Y"))
        uids = (data[0] or b"").split()[-25:]
        best: tuple[datetime, int, str] | None = None
        for uid in reversed(uids):
            _, raw = box.uid("fetch", uid, "(RFC822)")
            if not raw or not raw[0] or not isinstance(raw[0], tuple):
                continue
            msg = email.message_from_bytes(raw[0][1])
            try:
                when = parsedate_to_datetime(msg.get("Date"))
                when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if when < floor:
                continue
            subject = str(msg.get("Subject") or "")
            text = subject + " " + _body_text(msg)
            if not LOOKS_LIKE_CODE_MAIL.search(text):
                continue
            code = extract_code(text)
            link = extract_link(text, _body_html(msg))
            if not code and not link:
                continue
            low = text.lower()
            score = sum(1 for h in hints if h and h.lower() in low)
            if best is None or (score, when) > (best[1], best[0]):
                best = (when, score, {"code": code, "link": link})
        return best[2] if best else None
    finally:
        try:
            box.logout()
        except Exception:
            pass


def fetch_secret(since: float, hints: list[str], timeout_s: float = 240, poll_s: float = 10) -> dict | None:
    """Poll the inbox for a code or a sign-in link that arrived after `since`
    (a timestamp), preferring a message that names one of `hints`."""
    deadline = time.time() + timeout_s
    while True:
        try:
            found = _search_once(since, hints)
        except Exception:
            found = None
        if found or time.time() > deadline:
            return found
        time.sleep(poll_s)


def fetch_code(since: float, hints: list[str], timeout_s: float = 240, poll_s: float = 10) -> str | None:
    found = fetch_secret(since, hints, timeout_s, poll_s)
    return (found or {}).get("code")


async def fetch_code_async(since: float, hints: list[str], timeout_s: float = 240) -> str | None:
    return await asyncio.to_thread(fetch_code, since, hints, timeout_s)


async def fetch_secret_async(since: float, hints: list[str], timeout_s: float = 240) -> dict | None:
    return await asyncio.to_thread(fetch_secret, since, hints, timeout_s)
