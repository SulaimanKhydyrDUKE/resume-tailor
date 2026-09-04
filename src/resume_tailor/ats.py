"""Per-ATS conventions.

A listing URL is the posting. Where the application form actually lives is a
convention of the applicant-tracking system hosting it: Greenhouse puts the form
on the posting page, Lever puts it at `/apply`, Ashby at `/application`, and
Workday puts it behind an account. Knowing which is which decides both where
the runner navigates and whether a listing is worth attempting unattended.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

_KINDS = (
    ("greenhouse.io", "greenhouse"),
    ("lever.co", "lever"),
    ("ashbyhq.com", "ashby"),
    ("myworkdayjobs.com", "workday"),
    ("myworkdaysite.com", "workday"),
    ("smartrecruiters.com", "smartrecruiters"),
    ("icims.com", "icims"),
    ("oraclecloud.com", "oracle"),
    ("taleo.net", "taleo"),
    ("successfactors.com", "successfactors"),
    ("workable.com", "workable"),
    ("bamboohr.com", "bamboohr"),
    ("rippling.com", "rippling"),
    ("workatastartup.com", "workatastartup"),  # Y Combinator's board: apply with a YC account and profile
)

# Systems that gate every application behind a per-site account. The runner
# will reach them, hit the login wall, and log needs_login — correct, but each
# one is a job that needs you to log in once first, so they're sorted last.
NEEDS_ACCOUNT = {"workday", "icims", "oracle", "taleo", "successfactors", "workatastartup"}

# Systems whose forms this tool fills without help.
DIRECT_FORM = {"greenhouse", "lever", "ashby", "workable", "bamboohr", "rippling"}


def host_kind(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.netloc or "").lower()
    for needle, kind in _KINDS:
        if host.endswith(needle) or needle in host:
            return kind
    # A company careers site fronting a known system says so in its query
    # string (Westinghouse: "?ats=successfactors"; Cvent: "?icims=1").
    q = parts.query.lower()
    m = re.search(r"(?:^|&)ats=([a-z]+)", q)
    if m and m.group(1) in NEEDS_ACCOUNT | DIRECT_FORM:
        return m.group(1)
    m = re.search(r"(?:^|&)(icims|workday|successfactors|taleo|oracle|greenhouse|lever|ashby)=", q)
    if m:
        return m.group(1)
    return "other"


def apply_url_for(url: str) -> str:
    """The URL that carries the form, given the URL that carries the posting."""
    kind = host_kind(url)
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    if kind == "lever" and not path.endswith("/apply"):
        path += "/apply"
    elif kind == "ashby" and not path.endswith("/application"):
        path += "/application"
    else:
        return url
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))
