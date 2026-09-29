"""Instagram stories as a source of postings.

A page the user follows posts application links on its stories. Instagram has
no API for another account's stories and its web app never prints a sticker's
address — the sticker shows a label, the address sits behind it. So this reads
the stories the way a person would, in the tool's own signed-in Chrome (see
`resume-tailor login --site instagram.com`), and takes every link three ways:

1. the sticker is an anchor whose href is Instagram's redirect wrapper,
   `l.instagram.com/?u=<the address, encoded>` — decoded, no click;
2. the page's own story fetch returns JSON that names every sticker's
   address (`story_link_stickers[].story_link.url`) — captured off the wire;
3. failing both, the sticker is pressed and the tab it opens is read.

Short links and link-in-bio pages (Linktree and the like) are followed to the
posting; the posting page is read for its company and title (JSON-LD
JobPosting, then Open Graph, then the title) so the listing goes through the
same filters as any other source. Frames with no link are kept as screenshots
under output/instagram/<handle>/ for the user to look at.

Instagram forbids automated access and answers it with a "confirm it's you"
challenge on the account. This reader keeps to one pass every few hours, with
human pauses between frames, and on any challenge, login wall or rate-limit
page it stops and parks the source (`output/instagram-links.json`,
"parked") until a person has looked — it never retries into a ban. Stories
expire after 24 hours, which is why the cadence matters at all.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

LINKS_NAME = "instagram-links.json"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
# Link-in-bio and shortener hosts: the address behind them is the posting.
WRAPPERS = re.compile(r"^(www\.)?(linktr\.ee|linkin\.bio|beacons\.ai|lnk\.bio|stan\.store|bit\.ly|tinyurl\.com|t\.co|lnkd\.in|"
                      r"rb\.gy|shorturl\.at|cutt\.ly|tiny\.cc|ow\.ly|buff\.ly|is\.gd|forms\.gle)$", re.I)
# Links on a link-in-bio page that are not postings.
SKIP_LINK = re.compile(r"instagram\.com|facebook\.com|twitter\.com|x\.com/|tiktok\.com|youtube\.com|youtu\.be|snapchat|"
                       r"linkedin\.com/(in|company)/|discord\.(gg|com)|t\.me/|whatsapp|mailto:|tel:|/privacy|/terms|"
                       r"linktr\.ee/?$|\.(png|jpe?g|gif|svg|webp)$", re.I)
# What the story viewer shows when the session is gone or Instagram objects.
WALL = re.compile(r"suspicious login|confirm it'?s you|help us confirm|we detected unusual|try again later|"
                  r"challenge_required|log in to instagram|phone number, username, or email|/accounts/login|/challenge/", re.I)


# --- pure helpers ------------------------------------------------------------

def unwrap(url: str) -> str:
    """The address behind Instagram's redirect wrapper, and without tracking
    parameters; any other address as it is."""
    url = (url or "").strip()
    m = re.match(r"https?://l\.instagram\.com/\?(.*)$", url, re.I)
    if m:
        q = urllib.parse.parse_qs(m.group(1))
        url = (q.get("u") or [""])[0]
    parts = urllib.parse.urlsplit(url)
    if not parts.scheme:
        return url
    keep = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
            if not re.match(r"^(utm_|fbclid|igshid|igsh$|mc_cid|mc_eid|ref$|source$)", k, re.I)]
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(keep), ""))


def links_from_story_json(obj) -> list[str]:
    """Every sticker address in a story payload, whatever its shape: the
    current `story_link_stickers[].story_link.url`, the older
    `story_cta[].links[].webUri`, and any `link_url`/`web_uri` along the way."""
    out: list[str] = []

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k in ("url", "webUri", "web_uri", "link_url", "linkUrl") and isinstance(v, str) and v.startswith("http"):
                    out.append(v)
                else:
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(obj)
    seen: set[str] = set()
    result = []
    for u in out:
        u = unwrap(u)
        if u and "instagram.com" not in u and u not in seen:
            seen.add(u)
            result.append(u)
    return result


def is_wrapper(url: str) -> bool:
    host = urllib.parse.urlsplit(url or "").netloc.lower()
    return bool(WRAPPERS.match(host))


def links_from_html(html: str, base: str) -> list[str]:
    """The outward links of a link-in-bio page: every anchor that leaves the
    page and is not a social profile, a legal page or an image."""
    out: list[str] = []
    seen: set[str] = set()
    base_host = urllib.parse.urlsplit(base).netloc.lower()
    for m in re.finditer(r'href=["\']([^"\']+)["\']', html or "", re.I):
        href = unwrap(urllib.parse.urljoin(base, m.group(1).strip()))
        if not href.startswith("http") or SKIP_LINK.search(href):
            continue
        if urllib.parse.urlsplit(href).netloc.lower() == base_host:
            continue
        if href not in seen:
            seen.add(href)
            out.append(href)
    return out


def posting_facts(html: str, url: str) -> dict:
    """Company and title off a posting page: a JobPosting in JSON-LD first,
    then Open Graph, then the page title; the host stands in for a company
    the page does not name."""
    company, title = "", ""
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html or "", re.S | re.I):
        try:
            data = json.loads(m.group(1))
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for it in items:
            if not isinstance(it, dict):
                continue
            if isinstance(it.get("@graph"), list):
                items.extend(x for x in it["@graph"] if isinstance(x, dict))
            if str(it.get("@type", "")).lower() == "jobposting":
                title = title or str(it.get("title") or "")
                org = it.get("hiringOrganization")
                company = company or (str(org.get("name") or "") if isinstance(org, dict) else str(org or ""))
    if not title:
        m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', html or "", re.I) or \
            re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']', html or "", re.I)
        if m:
            title = m.group(1)
    if not company:
        m = re.search(r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)', html or "", re.I)
        if m:
            company = m.group(1)
    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.S | re.I)
        if m:
            title = re.sub(r"\s+", " ", m.group(1)).strip()
    title, company = _unescape(title), _unescape(company)
    # "Software Engineer Intern - Acme" / "Acme | Careers": split a title that carries the company.
    if title and not company:
        m = re.match(r"^(.*?)\s*[\-–|·@]\s*(.+?)$", title)
        if m and len(m.group(2)) < 40:
            title, company = m.group(1).strip(), m.group(2).strip()
    if not company:
        host = urllib.parse.urlsplit(url).netloc.lower()
        m = re.search(r"(?:boards|job-boards)\.greenhouse\.io/([^/]+)|jobs\.lever\.co/([^/]+)|jobs\.ashbyhq\.com/([^/]+)|"
                      r"^([a-z0-9-]+)\.(?:wd\d+\.myworkdayjobs|myworkdaysite)\.com", (host + urllib.parse.urlsplit(url).path).lower())
        if m:
            company = next(g for g in m.groups() if g).replace("-", " ").title()
        else:
            company = re.sub(r"^(www|careers|jobs|apply)\.", "", host).split(".")[0].title()
    title = _unescape(re.sub(r"\s*\|\s*(Careers|Jobs).*$", "", title, flags=re.I)).strip()
    return {"company": _unescape(company).strip()[:80], "title": title[:120]}


def _unescape(s: str) -> str:
    import html as _h
    return _h.unescape(s or "")


def listing_id(url: str) -> str:
    return "ig:" + hashlib.sha1(unwrap(url).encode("utf-8")).hexdigest()[:16]


# --- the store -----------------------------------------------------------------

def load_links(out_dir: str | Path) -> dict:
    p = Path(out_dir) / LINKS_NAME
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"pages": {}}


def save_links(out_dir: str | Path, data: dict) -> None:
    p = Path(out_dir) / LINKS_NAME
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(p)


def record_link(data: dict, handle: str, url: str, label: str = "", frame: int | None = None,
                via: str = "", chain: list[str] | None = None, facts: dict | None = None) -> bool:
    """Remember one address for one page; True when it is new."""
    page = data.setdefault("pages", {}).setdefault(handle, {"links": {}, "last_read": None, "parked": None, "frames_without_links": []})
    links = page.setdefault("links", {})
    url = unwrap(url)
    if url in links:
        return False
    links[url] = {"first_seen": time.strftime("%Y-%m-%dT%H:%M:%S"), "label": label[:80], "frame": frame, "via": via,
                  "chain": chain or [], **(facts or {})}
    return True


def to_listings(out_dir: str | Path, profile=None) -> list[dict]:
    """The stored addresses as listings for discovery: one per posting, with
    the company and title read off the page (or a placeholder that still
    reads as an internship, so the batch reads the posting itself)."""
    from .discover import title_terms

    data = load_links(out_dir)
    out: list[dict] = []
    seen: set[str] = set()
    for handle, page in (data.get("pages") or {}).items():
        for url, info in (page.get("links") or {}).items():
            if url in seen or is_wrapper(url) or SKIP_LINK.search(url):
                continue
            seen.add(url)
            title = info.get("title") or ""
            if not re.search(r"intern|co-?op", title, re.I):
                title = (title + " — Internship (from Instagram)").strip(" —") if title else "Internship (from Instagram)"
            try:
                posted = int(time.mktime(time.strptime(info.get("first_seen", "")[:19], "%Y-%m-%dT%H:%M:%S")))
            except Exception:
                posted = int(time.time())
            out.append({"id": listing_id(url), "url": url, "company_name": info.get("company") or "", "title": title,
                        "locations": [], "date_posted": posted, "source": f"instagram:{handle}", "active": True, "is_visible": True,
                        "terms": sorted(title_terms(title)) or ["Summer 2027"], "degrees": [], "category": "Software Engineering"})
    return out


def pages_from_profile(profile) -> list[str]:
    raw = ((getattr(profile, "answers", {}) or {}).get("search") or {}).get("instagram_pages") or []
    return [str(h).strip().lstrip("@").rstrip("/").split("/")[-1] for h in raw if str(h).strip()]


# --- following a link to the posting ---------------------------------------------

def _fetch(url: str, timeout: int = 15) -> tuple[str, str]:
    """(final url, html) after redirects, with a browser's user agent."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.geturl(), r.read(600_000).decode("utf-8", "replace")


def resolve(url: str, depth: int = 0) -> list[tuple[str, list[str], dict]]:
    """The postings behind an address: (posting url, the chain followed,
    facts). A shortener or a link-in-bio page can hold several."""
    url = unwrap(url)
    chain = [url]
    try:
        final, html = _fetch(url)
    except Exception as e:
        return [(url, chain, {"company": "", "title": "", "note": f"could not open: {str(e)[:60]}"})]
    final = unwrap(final)
    if final != url:
        chain.append(final)
    if is_wrapper(final) and depth < 2:
        found = []
        for sub in links_from_html(html, final)[:12]:
            for posting, sub_chain, facts in resolve(sub, depth + 1):
                found.append((posting, chain + sub_chain, facts))
        return found or [(final, chain, {"company": "", "title": "", "note": "a link page with nothing outward"})]
    return [(final, chain, posting_facts(html, final))]


# --- reading the stories ---------------------------------------------------------

_STORY_LINKS_JS = """() => {
  const out = [];
  for (const a of document.querySelectorAll('a[href]')) {
    const href = a.getAttribute('href') || '';
    if (href.includes('l.instagram.com') || (/^https?:/.test(href) && !href.includes('instagram.com')))
      out.push({href, label: (a.innerText || a.getAttribute('aria-label') || '').trim().slice(0, 80)});
  }
  return out;
}"""
_STICKER_JS = """() => {
  // A link sticker that is not an anchor: a button whose text names a site or says link.
  const out = [];
  for (const el of document.querySelectorAll('[role="button"], div[tabindex="0"]')) {
    const t = (el.innerText || el.getAttribute('aria-label') || '').trim();
    if (t && t.length < 90 && /link|\\.(com|io|co|ai|org|net|edu|ly|ee)\\b|apply|careers|jobs/i.test(t) && el.getBoundingClientRect().width > 40)
      out.push(t.slice(0, 80));
  }
  return out;
}"""
_FRAME_KEY_JS = """() => {
  const v = document.querySelector('video, img[srcset], img[src*="cdninstagram"]');
  const t = document.querySelector('time');
  return (v ? (v.currentSrc || v.src || v.getAttribute('srcset') || '') : '') + '|' + (t ? t.getAttribute('datetime') : '') + '|' + location.href;
}"""


def _park(data: dict, handle: str, reason: str) -> None:
    page = data.setdefault("pages", {}).setdefault(handle, {"links": {}, "last_read": None, "parked": None, "frames_without_links": []})
    page["parked"] = {"reason": reason, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}


async def read_page(handle: str, out_dir: str | Path, headless: bool = True, max_frames: int = 60, verbose: bool = True) -> dict:
    """One pass over a page's current stories. Returns a summary."""
    from .apply import ApplySession, DEFAULT_PROFILE_DIR

    out_dir = Path(out_dir)
    data = load_links(out_dir)
    page_rec = data.setdefault("pages", {}).setdefault(handle, {"links": {}, "last_read": None, "parked": None, "frames_without_links": []})
    if page_rec.get("parked"):
        return {"handle": handle, "parked": page_rec["parked"], "new": 0}
    shots = out_dir / "instagram" / handle
    shots.mkdir(parents=True, exist_ok=True)
    say = (lambda m: print(f"  [instagram:{handle}] {m}", file=sys.stderr, flush=True)) if verbose else (lambda m: None)
    session = ApplySession(headless=headless, fast=True, profile_dir=DEFAULT_PROFILE_DIR.parent / "login-profile")
    wire: list[str] = []
    summary = {"handle": handle, "frames": 0, "new": 0, "links": 0, "no_link_frames": 0, "parked": None}

    async def on_response(resp):
        try:
            url = resp.url
            if not re.search(r"reels_media|/stories/|graphql|/api/v1/feed/", url):
                return
            if "json" not in (resp.headers.get("content-type") or ""):
                return
            payload = await resp.json()
            for u in links_from_story_json(payload):
                if u not in wire:
                    wire.append(u)
        except Exception:
            pass

    try:
        await session.start()
        page = session._page
        page.on("response", lambda r: asyncio.ensure_future(on_response(r)))
        await session.goto(f"https://www.instagram.com/stories/{handle}/")
        await asyncio.sleep(random.uniform(3, 5))
        text = " ".join((await session.read_text())[:4000].split())
        if WALL.search(page.url) or WALL.search(text):
            reason = "Instagram wants a sign-in or a check — run `resume-tailor login --site instagram.com`, then `resume-tailor instagram unpark`"
            _park(data, handle, reason)
            await session.screenshot(shots / "parked.png")
            save_links(out_dir, data)
            summary["parked"] = reason
            return summary
        # "View story" sits between the profile and the first frame.
        for b in await session.buttons():
            if re.search(r"^\s*view story\s*$", b.get("text") or "", re.I):
                try:
                    await session.click(b["selector"])
                except Exception:
                    pass
                await asyncio.sleep(random.uniform(2, 3))
                break
        seen_keys: list[str] = []
        frame_links: dict[int, list[str]] = {}
        for i in range(max_frames):
            key = await page.evaluate(_FRAME_KEY_JS)
            if f"/stories/{handle}" not in page.url.lower():
                break
            if key in seen_keys[-3:]:
                break  # the viewer stopped advancing: the last frame
            seen_keys.append(key)
            summary["frames"] += 1
            found: list[tuple[str, str, str]] = []  # (url, label, via)
            for a in await page.evaluate(_STORY_LINKS_JS):
                found.append((unwrap(a["href"]), a.get("label") or "", "href"))
            fresh_wire = [u for u in wire if all(u != f[0] for f in found)]
            for u in fresh_wire:
                found.append((u, "", "wire"))
            if not found:
                stickers = await page.evaluate(_STICKER_JS)
                if stickers:
                    try:
                        async with session._ctx.expect_page(timeout=8000) as new_page_info:
                            await page.get_by_text(stickers[0], exact=False).first.click(timeout=4000)
                        new_page = await new_page_info.value
                        try:
                            await new_page.wait_for_load_state("domcontentloaded", timeout=8000)
                        except Exception:
                            pass
                        opened = unwrap(new_page.url)
                        await new_page.close()
                        if opened and "instagram.com" not in opened:
                            found.append((opened, stickers[0], "click"))
                    except Exception as e:
                        say(f"frame {i + 1}: the sticker '{stickers[0][:30]}' opened nothing ({str(e)[:50]})")
            if not found:
                shot = shots / f"{time.strftime('%Y%m%d')}-frame{i + 1:02d}.png"
                await session.screenshot(shot)
                page_rec.setdefault("frames_without_links", []).append({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "shot": str(shot)})
                page_rec["frames_without_links"] = page_rec["frames_without_links"][-40:]
                summary["no_link_frames"] += 1
            for url, label, via in found:
                if "instagram.com" in url:
                    continue
                summary["links"] += 1
                if url in page_rec.get("links", {}):
                    continue
                for posting, chain, facts in resolve(url):
                    if record_link(data, handle, posting, label=label, frame=i + 1, via=via, chain=chain, facts=facts):
                        summary["new"] += 1
                        say(f"frame {i + 1}: {facts.get('company') or '?'} — {facts.get('title') or posting[:60]}  ({via})")
            frame_links[i] = [f[0] for f in found]
            save_links(out_dir, data)
            await asyncio.sleep(random.uniform(2.5, 5.5))
            try:
                await page.keyboard.press("ArrowRight")
            except Exception:
                break
            await asyncio.sleep(random.uniform(1.0, 2.0))
            text = " ".join((await session.read_text())[:2000].split())
            if WALL.search(page.url) or WALL.search(text):
                _park(data, handle, "Instagram interrupted the stories with a check — sign in again by hand and unpark")
                summary["parked"] = page_rec["parked"]["reason"]
                break
        page_rec["last_read"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        page_rec["last_summary"] = {k: v for k, v in summary.items() if k != "handle"}
        save_links(out_dir, data)
    finally:
        try:
            await session.stop()
        except Exception:
            pass
    return summary


async def run_cli(args) -> int:
    from .profile import Profile

    out = Path(args.out)
    try:
        profile = Profile.load(getattr(args, "profile", None))
    except Exception:
        profile = None
    handles = [args.handle.lstrip("@")] if getattr(args, "handle", None) else pages_from_profile(profile)
    if args.action == "links":
        data = load_links(out)
        n = 0
        for handle, page in (data.get("pages") or {}).items():
            print(f"@{handle}: {len(page.get('links') or {})} links, last read {page.get('last_read') or 'never'}"
                  + (f", PARKED: {page['parked']['reason']}" if page.get("parked") else ""))
            for url, info in sorted((page.get("links") or {}).items(), key=lambda kv: kv[1].get("first_seen", ""), reverse=True):
                n += 1
                print(f"  {info.get('first_seen', '')[:16]}  {info.get('company') or '?':24.24s} {info.get('title') or '':40.40s}  {url[:70]}")
        if not n:
            print("nothing read yet — set search.instagram_pages in answers.yaml and run `resume-tailor instagram read`")
        return 0
    if args.action == "unpark":
        data = load_links(out)
        for h in handles or list((data.get("pages") or {}).keys()):
            if h in (data.get("pages") or {}):
                data["pages"][h]["parked"] = None
                print(f"@{h}: unparked")
        save_links(out, data)
        return 0
    if args.action == "watch":
        # A lane the supervisor keeps alive: with no page configured yet it
        # waits and re-reads the settings, rather than exiting every five minutes.
        while True:
            try:
                profile = Profile.load(getattr(args, "profile", None))
            except Exception:
                profile = None
            handles = [args.handle.lstrip("@")] if getattr(args, "handle", None) else pages_from_profile(profile)
            if not handles:
                print(f"[{time.strftime('%H:%M')}] instagram: no page configured (search.instagram_pages); waiting", file=sys.stderr, flush=True)
                await asyncio.sleep(30 * 60)
                continue
            for h in handles:
                try:
                    s = await read_page(h, out, headless=not getattr(args, "show", False))
                    print(f"[{time.strftime('%H:%M')}] @{h}: {s.get('frames', 0)} frames, {s.get('new', 0)} new"
                          + (f" — parked: {s['parked'][:80]}" if s.get("parked") else ""), file=sys.stderr, flush=True)
                except Exception as e:
                    print(f"[{time.strftime('%H:%M')}] @{h}: read failed: {str(e)[:120]}", file=sys.stderr, flush=True)
            await asyncio.sleep(max(30, int(args.interval)) * 60 + random.uniform(0, 600))
    if not handles:
        print("no page to read: set search.instagram_pages: [handle] in ~/.resume-tailor/answers.yaml, or pass --handle", file=sys.stderr)
        return 1
    if args.action == "read":
        rc = 0
        for h in handles:
            s = await read_page(h, out, headless=not getattr(args, "show", False))
            print(f"@{h}: {s.get('frames', 0)} frames, {s.get('links', 0)} links seen, {s.get('new', 0)} new, "
                  f"{s.get('no_link_frames', 0)} frames without a link" + (f" — PARKED: {s['parked']}" if s.get("parked") else ""), file=sys.stderr)
            rc = rc or (1 if s.get("parked") else 0)
        return rc
    return 1
