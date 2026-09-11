"""What came of each application, read from the applicant's own inbox.

Every application the loop sent produced mail: a confirmation, then maybe
an assessment invite, an interview request, an offer, or a rejection. This
module reads the mailbox over IMAP, ties each message to the company it is
about, decides which stage it announces, and keeps one timeline per company
in `output/results.json`. It also collects the recruiting addresses those
messages reveal (a coordinator's Reply-To, a campus team's inbox), which
the outreach module sends to.

Matching is deliberately narrow: a message belongs to a company when it
comes from that company's own domain, or from an applicant-tracking system
with the company named in it. A newsletter that happens to say "Apple" is
not an application update.

The stage is decided by phrase rules where the wording is unmistakable
("unfortunately", "invites you to take an assessment", "pleased to offer")
and by a model only for mail that is about a known company yet matches no
rule. Nothing the model has not been asked about leaves the machine.
"""
from __future__ import annotations

import email
import imaplib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

from .queue import company_key

STAGES = ("applied", "oa", "interview", "offer", "rejected")
RANK = {"applied": 1, "oa": 2, "interview": 3, "offer": 4}

# Where application mail comes from when it does not come from the company.
ATS_DOMAINS = (
    "greenhouse-mail.io", "greenhouse.io", "lever.co", "myworkday.com", "workday.com", "icims.com",
    "ashbyhq.com", "smartrecruiters.com", "oraclecloud.com", "oracle.com", "workablemail.com", "workable.com",
    "jobvite.com", "successfactors.com", "taleo.net", "paradox.ai", "hellosage.com", "candidate.fyi",
    "litmus.build", "coderbyte.com", "hackerrank.com", "codesignal.com", "codility.com", "criteriacorp.com",
    "bamboohr.com", "rippling.com", "applytojob.com", "yello.co", "eightfold.ai", "phenom.com", "brassring.com",
    "avature.net", "hireright.com", "goodtime.io", "calendly.com", "modernhire.com", "hirevue.com",
)
NOISE = re.compile(r"linkedin\.com|chronicle\.com|businessinsider|skool\.com|creativefabrica|coursera|chase\.com|"
                   r"google\.com|jobright\.ai|glassdoor|indeed\.com|handshake|simplify\.jobs|substack|medium\.com|"
                   r"github\.com|notion\.so|duke\.edu", re.I)
NOREPLY = re.compile(r"no-?reply|do-?not-?reply|donotreply|notification|mailer-daemon|postmaster|bounce|automated|"
                     r"noreply|system@|alerts?@|updates?@|info@|support@|hello@|team@|news", re.I)
RECRUITING_WORDS = re.compile(r"recruit|campus|universit|talent|career|intern|hiring|people|earlycareer|early-career|"
                              r"college|student|acquisition", re.I)
APPLICATION_WORDS = re.compile(r"applicat|candidate|position|role|intern|interview|assessment|offer|recruit|hiring|"
                               r"next steps?|résumé|resume", re.I)
CODE = re.compile(r"verification code|security code|one-time|passcode|verify your (email|identity)|confirm your email|"
                  r"\bOTP\b|sign-?in code|login code", re.I)
BOUNCE = re.compile(r"delivery status notification|undeliverable|delivery (has )?failed|mail delivery|address not found|"
                    r"couldn'?t be delivered", re.I)

# Unmistakable wording, in the order they are tried: the terminal verdict
# first so "unfortunately … after your interview" reads as a rejection.
RULES = (
    ("rejected", re.compile(
        r"unfortunately|not (be )?(moving|move|proceed(ing)?) forward|(pursue|move forward|proceed) with other (candidates|applicants)|"
        r"no longer (be )?(under )?consider|decided not to|not (been )?selected|will not be (moving|proceeding)|regret to inform|"
        r"unable to offer|position has been filled|not the right fit|we have chosen|other candidates whose|"
        r"not (to )?(advance|progress)|(closed|filled) the position|won'?t be (moving|proceeding)", re.I)),
    ("offer", re.compile(
        r"(pleased|excited|delighted|happy) to (offer|extend)|offer letter|formal offer|extend(ing)? (you )?an offer|"
        r"congratulations.{0,60}(offer|join)", re.I)),
    ("interview", re.compile(
        r"\binterview\b|phone screen|schedule (a|your) (call|conversation|chat|time)|calendly|book a time|"
        r"next step.{0,40}(call|conversation|chat)|hiring manager would like|would like to (speak|chat|talk) with you|"
        r"recruiter (call|screen)|first[- ]round|technical screen", re.I)),
    ("oa", re.compile(
        r"assessment|hackerrank|codesignal|codility|coderbyte|litmus|coding challenge|online test|take-?home|"
        r"criteria corp|aptitude test|complete the (test|challenge)|coding test|technical (test|challenge)", re.I)),
    ("applied", re.compile(
        r"thank you for (applying|your application|your interest|submitting)|thanks for (applying|your application)|"
        r"application (has been |was )?(received|submitted)|we('ve| have) received your application|received your application|"
        r"your application (to|for|has)|application confirmation|successfully (submitted|applied)|we got your application|"
        r"we'?ve got your application|application update|your recent application", re.I)),
)

AMBIGUOUS_NAME = re.compile(r"^(apple|scale|primer|cox|oscar|serval|netic|pylon|kastle|phoebe|valon|exa|hp|n1|apex|"
                            r"target|dell|intel|meta|square|block|stripe|ramp|brex|plaid|bolt|lyft|uber|dime|nash|"
                            r"paragon|abundant|magna|simon|humana|pilot|vertiv|verisk|premier|allegion|medline)$", re.I)


def _env() -> dict:
    try:
        from .llm import _load_env_file
        _load_env_file()
    except Exception:
        pass
    user = os.environ.get("RESUME_TAILOR_IMAP_USER") or ""
    if not user:
        try:
            from .mailbox import _profile_email
            user = _profile_email()  # the applicant's own address, as the applications use it
        except Exception:
            user = ""
    return {
        "host": os.environ.get("RESUME_TAILOR_IMAP_HOST", "imap.gmail.com"),
        "user": user,
        "password": (os.environ.get("RESUME_TAILOR_IMAP_PASSWORD") or "").replace(" ", ""),
    }


def configured() -> bool:
    e = _env()
    return bool(e["user"] and e["password"])


def _hdr(msg, name: str) -> str:
    try:
        return str(make_header(decode_header(msg.get(name, "") or ""))).strip()
    except Exception:
        return str(msg.get(name, "") or "").strip()


def _domain(addr: str) -> str:
    return addr.rsplit("@", 1)[-1].lower() if "@" in addr else ""


def _registrable(host: str) -> str:
    parts = host.lower().split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host.lower()


def _body_text(msg) -> str:
    plain, html = "", ""
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        if ctype == "text/plain" and not plain:
            plain = text
        elif ctype == "text/html" and not html:
            html = text
    if plain.strip():
        return plain
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", html, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"&nbsp;|&#160;", " ", text)


# ---------------------------------------------------------------------------
# Which company a message is about

def company_index(out_dir: Path) -> dict[str, dict]:
    """Every company the loop has touched: its key, the names it goes by,
    the domains its mail may come from, the tenant slugs its ATS uses."""
    state_path = out_dir / "batch-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {"done": {}}
    listings: dict[str, dict] = {}
    cache = out_dir / "listings-cache.json"
    if cache.is_file():
        for l in json.loads(cache.read_text(encoding="utf-8")):
            listings[l.get("id") or ""] = l
    index: dict[str, dict] = {}
    for eid, rec in (state.get("done") or {}).items():
        l = listings.get(eid, {})
        name = rec.get("company") or l.get("company_name") or ""
        key = company_key(name)
        if not key:
            continue
        co = index.setdefault(key, {"key": key, "names": set(), "domains": set(), "slugs": set(), "statuses": set(), "ids": []})
        co["names"].add(name)
        if l.get("company_name"):
            co["names"].add(l["company_name"])
        co["statuses"].add(rec.get("status") or "")
        co["ids"].append(eid)
        url = l.get("url") or ""
        host = (urlsplit(url).netloc or "").lower()
        if host:
            if any(host.endswith(a) for a in ATS_DOMAINS) or "myworkdayjobs" in host or "greenhouse" in host or "lever" in host:
                m = re.match(r"^([a-z0-9-]+)\.", host)
                if m and m.group(1) not in ("jobs", "boards", "job-boards", "careers", "apply", "www", "careers-", "app", "hire"):
                    co["slugs"].add(m.group(1))
                m = re.search(r"(?:greenhouse\.io|lever\.co|ashbyhq\.com|smartrecruiters\.com|workable\.com|applytojob\.com)/([a-z0-9._-]+)", url, re.I)
                if m:
                    co["slugs"].add(m.group(1).lower())
            else:
                co["domains"].add(_registrable(host))
        # The company's own domain guessed from its name, for mail that comes
        # from the company itself: "Datadog" -> datadog(hq).com is only a
        # guess, so it is matched as a substring of the sender's domain.
        co["slugs"].add(re.sub(r"[^a-z0-9]", "", key.split(" ")[0]) if key else "")
    for co in index.values():
        co["slugs"].discard("")
    return index


def _match_company(index: dict[str, dict], frm: str, reply_to: str, subject: str, body_head: str) -> str | None:
    sender_domain = _domain(parseaddr(frm)[1]) or _domain(parseaddr(reply_to)[1])
    from_ats = any(sender_domain.endswith(a) for a in ATS_DOMAINS)
    text = f"{parseaddr(frm)[0]} {subject} {body_head[:600]}"
    low = text.lower()
    hits: list[tuple[int, str]] = []
    for key, co in index.items():
        score = 0
        # From the company's own domain.
        if sender_domain and any(sender_domain == d or sender_domain.endswith("." + d) for d in co["domains"]):
            score = 3
        elif sender_domain and not from_ats and not NOISE.search(sender_domain):
            root = _registrable(sender_domain).split(".")[0]
            if root and len(root) >= 4 and any(root == s or root in s or s in root for s in co["slugs"] if len(s) >= 4):
                score = 3
        # Named in the text, with an ATS sender vouching for it.
        if not score:
            for name in co["names"]:
                n = name.lower().strip()
                if not n:
                    continue
                short = len(n) <= 4 or AMBIGUOUS_NAME.match(n)
                if re.search(r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])", low):
                    score = 2 if (from_ats or not short) else 0
                    if score and short and not from_ats:
                        score = 0
                    if score:
                        break
        if score:
            hits.append((score, key))
    if not hits:
        return None
    hits.sort(reverse=True)
    if len(hits) > 1 and hits[0][0] == hits[1][0] and hits[0][0] < 3:
        return None  # two companies named, neither by domain: not decidable
    return hits[0][1]


# ---------------------------------------------------------------------------
# Which stage a message announces

SUBJECT_APPLIED = re.compile(r"thank(s| you) for (applying|your (application|interest|submission))|application (received|confirmation|submitted)|"
                             r"we('ve| have)? received your (application|resume)|your application (to|for|has been|was)|"
                             r"successfully (submitted|applied)|we got your application|application (has been )?received", re.I)
# Body wording that means the stage on its own — never a conditional ("if
# selected for an interview") or a courtesy ("unfortunately we cannot reply
# to everyone"), which every confirmation carries.
BODY_RULES = (
    ("rejected", re.compile(
        r"(not|won'?t|will not) be (moving|proceeding|going) forward with your (application|candidacy)|"
        r"(decided|chosen|elected) to (move forward|proceed|go forward) with other (candidates|applicants)|"
        r"no longer (be )?(under )?consider(ed|ation)|regret to inform|unable to (offer|move forward)|"
        r"(have|has) (been )?(decided|chosen) not to|not (been )?selected (for|to)|position has been filled|"
        r"pursue other (candidates|applicants)|we will not be (moving|proceeding)|not be (advancing|progressing) your", re.I)),
    ("offer", re.compile(r"(pleased|excited|delighted|happy) to (offer|extend)|offer letter|formal offer|extend(ing)? (you )?an offer", re.I)),
    ("interview", re.compile(
        r"schedule (a|an|your) (interview|call|conversation|chat|time to)|invite you to (interview|an interview|a (phone|video|virtual)|schedule)|"
        r"would like to (interview|speak with|talk with|chat with) you|interview (has been|is) (scheduled|confirmed)|calendly\.com|"
        r"book a time|(next|first) step (is|will be) (a|an) (call|interview|conversation|phone)|phone screen|recruiter screen|"
        r"select a time|pick a time|availability for (a|an) (call|interview|conversation)", re.I)),
    ("oa", re.compile(
        r"invite(s|d)? you to (take|complete|start)|complete (the|your|an|this) (online )?(assessment|coding|test|challenge)|"
        r"assessment (invitation|link)|hackerrank|codesignal|codility|coderbyte|litmus|take-?home|coding challenge|"
        r"online assessment|technical assessment|aptitude test|assessment(s)? (has|have) been assigned", re.I)),
)


def _rule_stage(subject: str, body: str) -> str | None:
    """The stage the wording settles, or None for the model. The subject
    comes first: a confirmation is a confirmation whatever its body says
    in passing about interviews and other candidates."""
    if CODE.search(subject):
        return "code"
    for stage, pat in RULES:
        if stage != "applied" and pat.search(subject):
            return stage
    if SUBJECT_APPLIED.search(subject):
        return "applied"
    # A confirmation's courtesy sentences ("if you are not selected, keep an
    # eye on our jobs page", "if we choose not to move forward…") carry the
    # words of a rejection; they are struck before the body rules read it.
    head = re.sub(r"[^.!?\n]*\b(if (you are|you're|you were|we choose|we decide|your (skills|qualifications|profile|background)|"
                  r"selected|there is a match|we (find|see) a (fit|match)|a (fit|match) is)|keep an eye|should you (not )?be selected|"
                  r"unless (you are|selected)|not everyone|unable to (respond|reply) to (everyone|each|all))\b[^.!?\n]*[.!?]?",
                  " ", body[:2500], flags=re.I)
    for stage, pat in BODY_RULES:
        if pat.search(head):
            return stage
    return None


def _model_stage(company: str, frm: str, subject: str, body: str) -> dict:
    """gpt-4o reads one message about a known company and names its stage.
    Only messages the rules could not place get here."""
    try:
        from openai import OpenAI
        client = OpenAI()
        prompt = (
            "You sort e-mails received by a college student who applied to internships. "
            f"This message is about the company: {company}.\n\n"
            f"From: {frm}\nSubject: {subject}\n\n{body[:3500]}\n\n"
            "Reply with JSON: {\"stage\": one of \"applied\" (confirmation or generic status), \"oa\" (an assessment, coding test, "
            "or take-home to complete), \"interview\" (an interview or recruiter call is offered or scheduled), \"offer\", "
            "\"rejected\", or \"other\" (not about this application: marketing, event invite, account notice); "
            "\"date\": an ISO date or datetime the message gives for a deadline, assessment or interview, else null; "
            "\"note\": at most 12 words saying what the message asks or says}."
        )
        r = client.chat.completions.create(model=os.environ.get("RESUME_TAILOR_MAIL_MODEL", "gpt-4o"),
                                           messages=[{"role": "user", "content": prompt}],
                                           response_format={"type": "json_object"}, temperature=0)
        out = json.loads(r.choices[0].message.content or "{}")
        stage = str(out.get("stage") or "other").lower()
        return {"stage": stage if stage in STAGES or stage == "other" else "other",
                "date": out.get("date"), "note": str(out.get("note") or "")[:120], "by": "model"}
    except Exception as e:
        return {"stage": "other", "date": None, "note": f"model unavailable: {str(e)[:60]}", "by": "model-failed"}


# ---------------------------------------------------------------------------
# Addresses worth writing to

def _addresses(company_key_: str, frm: str, reply_to: str, body: str, own: str) -> list[dict]:
    found: list[dict] = []
    for source, raw in (("reply-to", reply_to), ("from", frm)):
        name, addr = parseaddr(raw)
        addr = addr.lower()
        if not addr or addr == own.lower() or NOREPLY.search(addr) or any(_domain(addr).endswith(a) for a in ATS_DOMAINS):
            continue
        found.append({"address": addr, "name": name.strip(), "source": source})
    for m in re.finditer(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", body[:6000]):
        addr = m.group(0).lower().rstrip(".")
        if addr == own.lower() or NOREPLY.search(addr) or any(_domain(addr).endswith(a) for a in ATS_DOMAINS):
            continue
        around = body[max(0, m.start() - 120): m.end() + 60]
        if RECRUITING_WORDS.search(addr.split("@")[0]) or RECRUITING_WORDS.search(around):
            found.append({"address": addr, "name": "", "source": "body"})
    seen: set[str] = set()
    out = []
    for f in found:
        if f["address"] not in seen:
            seen.add(f["address"])
            out.append(f)
    return out


# ---------------------------------------------------------------------------
# The scan

def load_results(out_dir: Path) -> dict:
    p = out_dir / "results.json"
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"last_uid": 0, "mailbox": "", "companies": {}, "bounced": [], "seen": {}}


def save_results(out_dir: Path, results: dict) -> None:
    tmp = out_dir / "results.json.tmp"
    tmp.write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(out_dir / "results.json")


def _company_stage(timeline: list[dict]) -> str:
    """The stage a company stands at: the latest verdict-bearing message
    wins when it is a rejection; otherwise the furthest stage reached."""
    stages = [t["stage"] for t in sorted(timeline, key=lambda t: t.get("when") or "") if t["stage"] in STAGES]
    if not stages:
        return ""
    if stages[-1] == "rejected":
        return "rejected"
    return max((s for s in stages if s != "rejected"), key=lambda s: RANK[s], default="applied")


def scan(out_dir: str | Path, since: str = "03-Sep-2026", mailbox: str = "[Gmail]/All Mail",
         use_model: bool = True, limit: int | None = None, verbose: bool = True) -> dict:
    """Read new mail, match, classify, and update results.json. Returns a
    summary of what this pass found."""
    out_dir = Path(out_dir)
    env = _env()
    if not (env["user"] and env["password"]):
        raise RuntimeError("the mailbox is not configured (RESUME_TAILOR_IMAP_USER / _PASSWORD)")
    results = load_results(out_dir)
    index = company_index(out_dir)
    own = env["user"]
    box = imaplib.IMAP4_SSL(env["host"])
    summary = {"new": 0, "matched": 0, "by_model": 0, "stages": {}, "bounces": 0}
    try:
        box.login(env["user"], env["password"])
        typ, _ = box.select(f'"{mailbox}"', readonly=True)
        if typ != "OK":
            box.select("INBOX", readonly=True)
            mailbox = "INBOX"
        if results.get("mailbox") != mailbox:
            results["last_uid"], results["mailbox"] = 0, mailbox
        typ, data = box.uid("search", None, f"(SINCE {since})")
        uids = [int(u) for u in (data[0].split() if data and data[0] else [])]
        uids = [u for u in uids if u > int(results.get("last_uid") or 0)]
        if limit:
            uids = uids[:limit]
        summary["new"] = len(uids)
        if not uids:
            return summary
        # Headers for everything new in one round trip; bodies only for what matched.
        rng = ",".join(str(u) for u in uids)
        typ, data = box.uid("fetch", rng, "(UID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE REPLY-TO)])")
        headers: dict[int, email.message.Message] = {}
        for item in data:
            if not isinstance(item, tuple):
                continue
            m = re.search(rb"UID (\d+)", item[0])
            if m:
                headers[int(m.group(1))] = email.message_from_bytes(item[1])
        def body_of(uid: int) -> str:
            """One message's text. Gmail drops a long IMAP session with
            "socket error: EOF"; reconnect once and read on."""
            nonlocal box
            for attempt in range(2):
                try:
                    typ, bd = box.uid("fetch", str(uid), "(BODY.PEEK[])")
                    return _body_text(email.message_from_bytes(bd[0][1])) if bd and isinstance(bd[0], tuple) else ""
                except (imaplib.IMAP4.abort, OSError) as e:
                    if attempt:
                        raise
                    print(f"  mailbox connection dropped ({str(e)[:60]}); reconnecting", file=sys.stderr, flush=True)
                    try:
                        box.logout()
                    except Exception:
                        pass
                    box = imaplib.IMAP4_SSL(env["host"])
                    box.login(env["user"], env["password"])
                    box.select(f'"{mailbox}"', readonly=True)
            return ""

        processed = 0
        for uid in uids:
            h = headers.get(uid)
            if h is None:
                continue
            processed += 1
            if processed % 25 == 0:
                # Progress survives a crash: what is matched so far is on
                # disk, and the next run starts after the last message read.
                results["last_uid"] = max(int(results.get("last_uid") or 0), uid)
                results["scanned_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                save_results(out_dir, results)
            frm, subject, reply_to = _hdr(h, "From"), _hdr(h, "Subject"), _hdr(h, "Reply-To")
            try:
                when = parsedate_to_datetime(h.get("Date")).astimezone(timezone.utc).isoformat(timespec="minutes")
            except Exception:
                when = ""
            sender_domain = _domain(parseaddr(frm)[1])
            if BOUNCE.search(subject) or "mailer-daemon" in frm.lower():
                body = body_of(uid)
                for m in re.finditer(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", body[:4000]):
                    addr = m.group(0).lower()
                    if addr != own.lower() and "mailer-daemon" not in addr and addr not in results["bounced"]:
                        results["bounced"].append(addr)
                        summary["bounces"] += 1
                continue
            if NOISE.search(sender_domain) and not any(sender_domain.endswith(a) for a in ATS_DOMAINS):
                continue
            key = _match_company(index, frm, reply_to, subject, "")
            body = ""
            if key is None and APPLICATION_WORDS.search(subject):
                body = body_of(uid)
                key = _match_company(index, frm, reply_to, subject, body)
            if key is None:
                continue
            if not body:
                body = body_of(uid)
            stage = _rule_stage(subject, body)
            entry = {"uid": uid, "when": when, "from": frm[:80], "subject": subject[:140], "stage": stage or "other", "by": "rule"}
            if stage == "code":
                entry["stage"] = "other"
                entry["note"] = "verification code"
            elif stage is None and use_model and APPLICATION_WORDS.search(subject + " " + body[:1500]):
                verdict = _model_stage(index[key]["names"] and sorted(index[key]["names"])[0] or key, frm, subject, body)
                entry.update({"stage": verdict["stage"], "by": verdict["by"], "note": verdict.get("note", "")})
                if verdict.get("date"):
                    entry["date"] = verdict["date"]
                summary["by_model"] += 1
            co = results["companies"].setdefault(key, {"company": sorted(index[key]["names"])[0], "stage": "", "timeline": [], "addresses": []})
            co["company"] = sorted(index[key]["names"], key=len)[0]
            if not any(t["uid"] == uid for t in co["timeline"]):
                co["timeline"].append(entry)
            for a in _addresses(key, frm, reply_to, body, own):
                if not any(x["address"] == a["address"] for x in co["addresses"]):
                    a.update({"subject": subject[:80], "when": when})
                    co["addresses"].append(a)
            co["stage"] = _company_stage(co["timeline"])
            summary["matched"] += 1
            summary["stages"][entry["stage"]] = summary["stages"].get(entry["stage"], 0) + 1
            if verbose and entry["stage"] not in ("applied", "other"):
                print(f"  {entry['stage']:9s} {co['company'][:28]:28s} {subject[:70]}", file=sys.stderr, flush=True)
            results["last_uid"] = max(int(results.get("last_uid") or 0), uid)
        results["last_uid"] = max(int(results.get("last_uid") or 0), max(uids))
        results["scanned_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        save_results(out_dir, results)
    finally:
        try:
            box.logout()
        except Exception:
            pass
    return summary


# ---------------------------------------------------------------------------
# What the dashboard draws

def flows(out_dir: str | Path) -> dict:
    """Two flows for the results page: every attempt by outcome, and every
    company applied to by what the inbox says came of it."""
    out_dir = Path(out_dir)
    state_path = out_dir / "batch-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {"done": {}}
    done = state.get("done") or {}
    by_status: dict[str, int] = {}
    applied_keys: set[str] = set()
    for rec in done.values():
        st = rec.get("status") or "?"
        by_status[st] = by_status.get(st, 0) + 1
        if st in ("applied", "by_hand"):
            k = company_key(rec.get("company") or "")
            if k:
                applied_keys.add(k)
    results = load_results(out_dir)
    stages: dict[str, int] = {"offer": 0, "interview": 0, "oa": 0, "rejected": 0, "no reply yet": 0}
    companies = []
    for k in sorted(applied_keys):
        co = results["companies"].get(k)
        stage = (co or {}).get("stage") or ""
        bucket = stage if stage in ("offer", "interview", "oa", "rejected") else "no reply yet"
        stages[bucket] += 1
        if co:
            companies.append({"key": k, "company": co["company"], "stage": stage or "applied",
                              "last": max((t.get("when") or "" for t in co["timeline"]), default=""),
                              "mails": len(co["timeline"]),
                              "dates": [t["date"] for t in co["timeline"] if t.get("date")]})
    companies.sort(key=lambda c: (-(RANK.get(c["stage"], 0) if c["stage"] != "rejected" else -1), c["company"]))
    return {"attempts": {"total": len(done), "by_status": by_status},
            "applied": {"total": len(applied_keys), "by_stage": stages},
            "companies": companies, "scanned_at": results.get("scanned_at", "")}


def run_cli(args) -> int:
    out = Path(args.out)
    if args.action == "scan":
        s = scan(out, since=args.since, use_model=not args.no_model, limit=args.limit)
        print(f"new mail: {s['new']}, matched to a company: {s['matched']}, read by the model: {s['by_model']}, "
              f"stages: {s['stages']}, bounces: {s['bounces']}", file=sys.stderr)
        return 0
    if args.action == "results":
        f = flows(out)
        print(f"attempts: {f['attempts']['total']} {f['attempts']['by_status']}")
        print(f"companies applied to: {f['applied']['total']} {f['applied']['by_stage']}")
        for c in f["companies"]:
            if c["stage"] not in ("applied", ""):
                print(f"  {c['stage']:9s} {c['company'][:32]:32s} {c['last'][:16]}  {'; '.join(c['dates'][:2])}")
        return 0
    if args.action == "watch":
        while True:
            try:
                s = scan(out, since=args.since, use_model=not args.no_model)
                print(f"[{time.strftime('%H:%M')}] mail: {s['new']} new, {s['matched']} matched, stages {s['stages']}", file=sys.stderr, flush=True)
            except Exception as e:
                print(f"[{time.strftime('%H:%M')}] mail scan failed: {str(e)[:120]}", file=sys.stderr, flush=True)
            time.sleep(max(60, int(args.interval * 60)))
    return 1
