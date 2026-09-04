"""Model providers.

The chain above this is provider-agnostic: every model call is "a stable
system prefix plus one user prompt, returning one validated object". Which
model answers is chosen here, from the environment:

  RESUME_TAILOR_PROVIDER   anthropic (default) | openai
  RESUME_TAILOR_MODEL      override the provider's default model id

Both, and the API keys, may also be set in ~/.resume-tailor/env — one KEY=value
per line — which is read before the provider is chosen. A value already in the
environment wins over the file.

Credentials are whatever each SDK resolves on its own. For Anthropic that is an
ANTHROPIC_API_KEY or an `ant auth login` profile — and a set key, even an empty
or placeholder one, shadows the profile. For OpenAI it is OPENAI_API_KEY.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from typing import Awaitable, Callable, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

DEFAULT_MODELS = {"anthropic": "claude-opus-5", "openai": "gpt-5.2"}

_RETRY_IN = re.compile(r"try again in ([\d.]+)\s*(ms|s)\b", re.I)
_MAX_ATTEMPTS = 8


def _retry_delay(err: BaseException, attempt: int) -> float:
    """How long a 429 asks us to wait: the retry-after headers if present, the
    "try again in 17.2s" phrase in the body if not, exponential backoff capped
    at a minute otherwise. A small buffer is added because the server's number
    is when the window opens, not when a request will succeed."""
    headers = getattr(getattr(err, "response", None), "headers", None) or {}
    for key, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        raw = headers.get(key)
        if raw:
            try:
                return float(raw) * scale + 0.3
            except ValueError:
                pass
    m = _RETRY_IN.search(str(err))
    if m:
        value, unit = float(m.group(1)), m.group(2).lower()
        return (value / 1000 if unit == "ms" else value) + 0.3
    return min(60.0, 2.0 ** attempt)


class _Pacer:
    """Shared by every call to one provider: caps how many requests are in
    flight, and after a 429 holds everyone back until the window the server
    named has passed — so five parallel drafts do not take turns discovering
    the same limit. Lets the chain run unchanged on a 10k-tokens-per-minute
    free tier; it just takes longer."""

    def __init__(self) -> None:
        self._loop = None
        self._sem: asyncio.Semaphore | None = None
        self.cooldown_until = 0.0

    def semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if self._sem is None or self._loop is not loop:
            n = int(os.environ.get("RESUME_TAILOR_CONCURRENCY", "4") or 4)
            self._loop, self._sem = loop, asyncio.Semaphore(max(1, n))
        return self._sem

    async def run(self, label: str, call: Callable[[], Awaitable[T]],
                  rate_limit_error: type[BaseException]) -> T:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            delay = self.cooldown_until - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            async with self.semaphore():
                try:
                    return await call()
                except rate_limit_error as e:
                    if attempt == _MAX_ATTEMPTS:
                        raise RuntimeError(
                            f"{label} rate limit persisted through {attempt} attempts: {e}"
                        ) from e
                    wait = _retry_delay(e, attempt)
                    self.cooldown_until = max(self.cooldown_until, time.monotonic() + wait)
                    print(f"  {label} rate limit hit — waiting {wait:.0f}s ({attempt}/{_MAX_ATTEMPTS})…",
                          file=sys.stderr)
        raise AssertionError("unreachable")


class LLM(Protocol):
    name: str
    model: str

    async def parse(self, system: list[dict], prompt: str, schema: type[T], effort: str = "high") -> T: ...


class AnthropicLLM:
    name = "anthropic"

    def __init__(self, model: str) -> None:
        self.model = model
        self._client = None
        self._pacer = _Pacer()

    def _c(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.AsyncAnthropic()
        return self._client

    async def parse(self, system: list[dict], prompt: str, schema: type[T], effort: str = "high") -> T:
        import anthropic

        async def call():
            return await self._c().messages.parse(
                model=self.model, max_tokens=16000, system=system,
                thinking={"type": "adaptive"}, output_config={"effort": effort},
                messages=[{"role": "user", "content": prompt}], output_format=schema,
            )

        try:
            resp = await self._pacer.run("Anthropic", call, anthropic.RateLimitError)
        except anthropic.NotFoundError as e:
            raise RuntimeError(
                f"Anthropic model {self.model!r} not found. Set RESUME_TAILOR_MODEL to one your "
                f"account can use. {e}"
            ) from e
        return resp.parsed_output


class OpenAILLM:
    name = "openai"
    # Models that accept reasoning_effort. Passing it to one that does not is a 400.
    _REASONING_PREFIXES = ("gpt-5", "o1", "o3", "o4")

    def __init__(self, model: str) -> None:
        self.model = model
        self._client = None
        self._pacer = _Pacer()

    def _c(self):
        if self._client is None:
            import openai

            # A request that never returns stalled a whole overnight pass for 95
            # minutes; four minutes is longer than any structured call here takes.
            self._client = openai.AsyncOpenAI(timeout=240.0, max_retries=2)
        return self._client

    async def parse(self, system: list[dict], prompt: str, schema: type[T], effort: str = "high") -> T:
        import openai

        # The Anthropic-shaped system blocks collapse into one system message.
        # Their cache_control markers are dropped: OpenAI caches a stable prefix
        # of 1024+ tokens on its own, and the career record is exactly that.
        system_text = "\n\n".join(b["text"] for b in system if b.get("type") == "text")
        kwargs: dict = {}
        if self.model.startswith(self._REASONING_PREFIXES):
            kwargs["reasoning_effort"] = effort if effort in ("low", "medium", "high") else "medium"
        async def call():
            return await self._c().chat.completions.parse(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_text},
                    {"role": "user", "content": prompt},
                ],
                response_format=schema,
                max_completion_tokens=16000,
                **kwargs,
            )

        try:
            resp = await self._pacer.run("OpenAI", call, openai.RateLimitError)
        except openai.NotFoundError as e:
            raise RuntimeError(
                f"OpenAI model {self.model!r} not found. Set RESUME_TAILOR_MODEL to one your "
                f"account can use (platform.openai.com/docs/models). {e}"
            ) from e
        choice = resp.choices[0]
        msg = choice.message
        if getattr(msg, "refusal", None):
            raise RuntimeError(f"{self.model} refused the request: {msg.refusal}")
        if msg.parsed is None:
            raise RuntimeError(
                f"{self.model} returned no parseable output (finish_reason={choice.finish_reason})"
            )
        return msg.parsed


_PLACEHOLDERS = {"", "PASTE-YOUR-KEY-HERE"}


def _load_env_file() -> None:
    """~/.resume-tailor/env — a file to put a key in, instead of a shell export
    to remember. Untouched placeholders are ignored so a forgotten field fails
    as "not set" rather than as a rejected credential."""
    from .profile import DEFAULT_PROFILE_DIR

    path = DEFAULT_PROFILE_DIR / "env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.split(" #", 1)[0].strip().strip("'\"")
        key = key.strip()
        if key and value not in _PLACEHOLDERS:
            os.environ.setdefault(key, value)


_llms: dict[str, LLM] = {}


def get_llm(role: str = "main") -> LLM:
    """`main` reads, plans and drafts; `audit` runs the many small entailment
    checks. RESUME_TAILOR_AUDIT_MODEL sends those to a different model of the
    same provider — the audits are two-thirds of a run's calls, so pointing
    them at a model with a roomier rate limit is what keeps a tight tier from
    stalling the run."""
    if role not in _llms:
        _load_env_file()
        provider = os.environ.get("RESUME_TAILOR_PROVIDER", "anthropic").strip().lower()
        if provider not in DEFAULT_MODELS:
            raise RuntimeError(
                f"RESUME_TAILOR_PROVIDER={provider!r}; expected one of: {', '.join(DEFAULT_MODELS)}"
            )
        model = os.environ.get("RESUME_TAILOR_MODEL", "").strip() or DEFAULT_MODELS[provider]
        if role != "main":
            # RESUME_TAILOR_AUDIT_MODEL, RESUME_TAILOR_JUDGE_MODEL, ...
            model = os.environ.get(f"RESUME_TAILOR_{role.upper()}_MODEL", "").strip() or model
        if provider == "openai" and not os.environ.get("OPENAI_API_KEY"):
            from .profile import DEFAULT_PROFILE_DIR

            raise RuntimeError(
                "OPENAI_API_KEY is not set. Put it on the OPENAI_API_KEY= line in "
                f"{DEFAULT_PROFILE_DIR / 'env'}, or export it in your shell."
            )
        _llms[role] = AnthropicLLM(model) if provider == "anthropic" else OpenAILLM(model)
    return _llms[role]
