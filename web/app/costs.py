"""What an AI analysis cost: the token usage each API response reports, times the price list below.

Every call to an AI service (claude._ask_claude for Anthropic, gpt.run_until_tool_call for OpenAI) hands its
response to record(), once: a continued conversation (a helper's answer, a nudge to call the tool) is a new response
with its own usage, so it counts again. An analysis (recognising a piece, reading an order, the stylist) runs under a
meter (main._analysis), which collects the calls it made and keeps them as one row in ai_usage. These are estimates:
usage × list price. What the service really bills is in its own console.
(The same as CellarPeaks' costs.py, without the web searches Dressing doesn't use.)
"""

import contextvars
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime

from . import db

# MARK: - Prices (the one place to change them)

# USD per million tokens, for each kind of token separately: input (uncached), cached (read from the cache),
# cache_write (written to the cache) and output. A kind without a price is left out of the cost, and the analysis is
# "partial". A model's id must be listed exactly (a date at the end, as in "claude-opus-5-20260301", is ignored).
# A model not listed: its tokens are kept, its cost is unknown.
# Overrides without touching the code: DATA_DIR/prices.json, e.g.
# {"openai": {"gpt-6-luna": {"input": 0.1, "cached": 0.01, "cache_write": 0.125, "output": 0.5}}}
PRICES = {
    # Anthropic's list prices (platform.claude.com/docs/en/about-claude/pricing, checked October 2026): the app's cache
    # writes are the 5-minute kind; the 1-hour kind costs 2x input.
    "anthropic": {
        "claude-fable-5-1": {"input": 10.0, "cached": 0.25, "cache_write": 12.5, "cache_write_1h": 20.0, "output": 50.0},
        "claude-fable-5": {"input": 10.0, "cached": 1.0, "cache_write": 12.5, "cache_write_1h": 20.0, "output": 50.0},
        "claude-opus-5-5": {"input": 4.0, "cached": 0.20, "cache_write": 5.0, "cache_write_1h": 8.0, "output": 20.0},
        "claude-opus-5": {"input": 5.0, "cached": 0.50, "cache_write": 6.25, "cache_write_1h": 10.0, "output": 25.0},
        "claude-opus-4-8": {"input": 5.0, "cached": 0.50, "cache_write": 6.25, "cache_write_1h": 10.0, "output": 25.0},
        "claude-opus-4-7": {"input": 5.0, "cached": 0.50, "cache_write": 6.25, "cache_write_1h": 10.0, "output": 25.0},
        "claude-opus-4-6": {"input": 5.0, "cached": 0.50, "cache_write": 6.25, "cache_write_1h": 10.0, "output": 25.0},
        "claude-sonnet-5-5": {"input": 2.0, "cached": 0.10, "cache_write": 2.50, "cache_write_1h": 4.0, "output": 10.0},
        "claude-sonnet-5": {"input": 2.0, "cached": 0.20, "cache_write": 2.50, "cache_write_1h": 4.0, "output": 10.0},
        "claude-sonnet-4-6": {"input": 3.0, "cached": 0.30, "cache_write": 3.75, "cache_write_1h": 6.0, "output": 15.0},
        "claude-sonnet-4-5": {"input": 3.0, "cached": 0.30, "cache_write": 3.75, "cache_write_1h": 6.0, "output": 15.0},
        # Haiku 5.5 is priced by prompt length: a prompt of over 100,000 tokens pays 5x, for every kind of token.
        "claude-haiku-5-5": {"input": 0.10, "cached": 0.01, "cache_write": 0.125, "cache_write_1h": 0.20, "output": 0.50,
                             "long_context": {"above": 100_000, "input": 5.0, "output": 5.0}},
        "claude-haiku-4-5": {"input": 1.0, "cached": 0.10, "cache_write": 1.25, "cache_write_1h": 2.0, "output": 5.0},
    },
    # OpenAI's pricing page (developers.openai.com/api/docs/pricing), Standard tier, checked October 2026. Above 272K
    # input tokens a whole request costs 2x the input and cache prices and 1.5x the output price.
    "openai": {
        "gpt-6-astra": {"input": 10.0, "cached": 1.0, "cache_write": 12.50, "output": 50.0,
                        "long_context": {"above": 272_000, "input": 2.0, "output": 1.5}},
        # An image model: text and photos in at their own price, the picture out. Cached input only counts in the
        # Responses API, which the app doesn't use for pictures.
        "gpt-image-2.5-flare": {"input": 5.0, "image_input": 8.0, "output": 30.0},
        "gpt-6-luna": {"input": 0.10, "cached": 0.01, "cache_write": 0.125, "output": 0.50,
                       "long_context": {"above": 272_000, "input": 2.0, "output": 1.5}},
        "gpt-6.1-sol": {"input": 2.0, "cached": 0.10, "cache_write": 2.50, "output": 10.0,
                        "long_context": {"above": 272_000, "input": 2.0, "output": 1.5}},
        "gpt-6-sol": {"input": 2.0, "cached": 0.20, "cache_write": 2.50, "output": 10.0,
                      "long_context": {"above": 272_000, "input": 2.0, "output": 1.5}},
    },
}
# Both services bill in dollars; the app shows euros. Late September 2026: 1 USD = 0.87 EUR. An estimate.
USD_TO_EUR = float(os.environ.get("USD_EUR", "0.87"))
STANDARD_TIERS = {None, "", "default", "auto", "standard"}  # the service tier the prices above are for
_DATED = re.compile(r"-(\d{8}|\d{4}-\d{2}-\d{2})$")


def _overrides() -> None:
    file = db.DATA_DIR / "prices.json"
    try:
        extra = json.loads(file.read_text()) if file.exists() else {}
    except (OSError, ValueError):
        print(f"{file} is not valid JSON: the built-in prices are used")
        return
    number = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0
    for provider, models in extra.items() if isinstance(extra, dict) else ():
        if provider not in PRICES or not isinstance(models, dict):
            continue
        for name, p in models.items():
            if not isinstance(p, dict):
                continue
            # What's given changes; what isn't stays (a new model needs at least input and output).
            merged = {**PRICES[provider].get(name, {}), **{k: v for k, v in p.items() if number(v)}}
            long = p.get("long_context")
            if isinstance(long, dict) and all(number(long.get(k)) for k in ("above", "input", "output")):
                merged["long_context"] = long
            if number(merged.get("input")) and number(merged.get("output")):
                PRICES[provider][name] = merged
            else:
                print(f"prices.json: {provider} {name} needs a number for input and output; ignored")


def price(provider: str, model: str) -> dict | None:
    return PRICES.get(provider, {}).get(_DATED.sub("", model or ""))


# MARK: - One API call

@dataclass
class Call:
    provider: str
    model: str
    # input_tokens is the WHOLE prompt of the response: uncached, read from the cache and written to it.
    input_tokens: int = 0
    cached_input_tokens: int = 0  # read from the cache
    cache_write_tokens: int = 0  # written to the cache (all kinds)
    cache_write_1h_tokens: int = 0  # of those, Anthropic's 1-hour cache
    output_tokens: int = 0  # reasoning included: billed as output
    cost_usd: float = 0.0
    # estimated: all usage and prices known · partial: a part couldn't be priced or wasn't reported (listed in
    # `missing`; the cost is then lower than the bill) · unknown: no usable usage, or no price.
    status: str = "estimated"
    missing: list = field(default_factory=list)


def _get(obj, name):
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def _count(obj, name) -> int | None:
    """A token count from the usage object (or dict); None when the field isn't there."""
    value = _get(obj, name) if obj is not None else None
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _flag(call: Call, reason: str) -> Call:
    if reason not in call.missing:
        call.missing.append(reason)
    if call.status == "estimated":
        call.status = "partial"
    return call


def _priced(call: Call) -> Call:
    """The cost from each kind of token at its own price; a kind with tokens but no price is left out."""
    p = price(call.provider, call.model)
    if p is None:
        call.status, call.missing = "unknown", [*call.missing, "prijs van het model"]
        return call
    f_in = f_out = 1.0
    if (long := p.get("long_context")) and call.input_tokens > long["above"]:
        f_in, f_out = long["input"], long["output"]
    plain = max(call.input_tokens - call.cached_input_tokens - call.cache_write_tokens, 0)
    cost = plain * p["input"] * f_in + call.output_tokens * p["output"] * f_out
    for tokens, key, label in ((call.cached_input_tokens, "cached", "prijs van cached input"),
                               (call.cache_write_tokens - call.cache_write_1h_tokens, "cache_write", "prijs van cache writes"),
                               (call.cache_write_1h_tokens, "cache_write_1h", "prijs van 1-uur cache writes")):
        if tokens and key in p:
            cost += tokens * p[key] * f_in
        elif tokens:
            _flag(call, label)
    call.cost_usd = cost / 1_000_000
    return call


def from_anthropic(model: str, usage) -> Call:
    """Anthropic's usage: input_tokens is only the uncached rest of the prompt; cache_creation_input_tokens and
    cache_read_input_tokens come on top."""
    call = Call("anthropic", model)
    plain, out = _count(usage, "input_tokens"), _count(usage, "output_tokens")
    if plain is None or out is None:
        call.status, call.missing = "unknown", ["tokengebruik"]
        return call
    call.cache_write_tokens = _count(usage, "cache_creation_input_tokens") or 0
    call.cached_input_tokens = _count(usage, "cache_read_input_tokens") or 0
    call.cache_write_1h_tokens = min(_count(_get(usage, "cache_creation"), "ephemeral_1h_input_tokens") or 0, call.cache_write_tokens)
    call.input_tokens = plain + call.cache_write_tokens + call.cached_input_tokens
    call.output_tokens = out
    call = _priced(call)
    if _get(usage, "service_tier") not in STANDARD_TIERS:
        _flag(call, f"service tier {_get(usage, 'service_tier')}: andere prijzen")
    return call


def anthropic_calls(requested: str, served: str, usage) -> list[Call]:
    """One response as the calls it was billed as. After a refusal fallback (the app asks for one with
    fallbacks="default") usage.iterations has every attempt with its own model and tokens."""
    iterations = _get(usage, "iterations") or []
    if not any(_get(it, "type") == "fallback_message" for it in iterations):
        return [from_anthropic(served, usage)]
    calls = [from_anthropic(_get(it, "model") or requested, it) for it in iterations
             if _get(it, "type") in ("message", "fallback_message")]
    _flag(calls[0], "fallback na een weigering: of de geweigerde poging aangerekend wordt, is niet zeker")
    return calls


def from_openai(model: str, usage, service_tier: str | None = None) -> Call:
    """OpenAI's Responses API usage: input_tokens is the whole prompt; input_tokens_details.cached_tokens and
    .cache_write_tokens are parts of it; output_tokens includes the reasoning tokens."""
    call = Call("openai", model)
    inp, out = _count(usage, "input_tokens"), _count(usage, "output_tokens")
    if inp is None or out is None:
        call.status, call.missing = "unknown", ["tokengebruik"]
        return call
    call.input_tokens, call.output_tokens = inp, out
    details = _get(usage, "input_tokens_details")
    cached, written = _count(details, "cached_tokens"), _count(details, "cache_write_tokens")
    call.cached_input_tokens, call.cache_write_tokens = cached or 0, written or 0
    call = _priced(call)
    if cached is None:
        _flag(call, "cached tokens niet gerapporteerd")
    if written is None and "cache_write" in (price("openai", model) or {}):
        _flag(call, "cache writes niet gerapporteerd")
    if service_tier not in STANDARD_TIERS:
        _flag(call, f"service tier {service_tier}: andere prijzen")
    return call


# MARK: - The meter of one analysis

METER: contextvars.ContextVar[list | None] = contextvars.ContextVar("ai_meter", default=None)


def record(call: Call) -> None:
    """Adds one API call to the analysis that is running. Outside an analysis it's not kept."""
    if (calls := METER.get()) is not None:
        calls.append(call)


# The ways the AI code reports a call. They never raise: what a call cost must never make it fail.

def track_anthropic(requested: str, response) -> None:
    try:
        served = getattr(response, "model", None) or requested  # a fallback can answer with another model
        for call in anthropic_calls(requested, served, getattr(response, "usage", None)):
            record(call)
    except Exception as e:
        print(f"cost of an Anthropic call: {e!r}")
        record(Call("anthropic", requested, status="unknown", missing=["kost kon niet berekend worden"]))


def track_openai(requested: str, response) -> None:
    try:
        record(from_openai(getattr(response, "model", None) or requested, getattr(response, "usage", None),
                           getattr(response, "service_tier", None)))
    except Exception as e:
        print(f"cost of an OpenAI call: {e!r}")
        record(Call("openai", requested, status="unknown", missing=["kost kon niet berekend worden"]))


def from_openai_image(model: str, usage) -> Call:
    """The Images API's usage: input_tokens split in text_tokens and image_tokens (the photos sent along), each at its
    own price; output_tokens is the picture."""
    call = Call("openai", model)
    inp, out = _count(usage, "input_tokens"), _count(usage, "output_tokens")
    p = price("openai", model)
    if inp is None or out is None:
        call.status, call.missing = "unknown", ["tokengebruik"]
        return call
    call.input_tokens, call.output_tokens = inp, out
    if p is None:
        call.status, call.missing = "unknown", ["prijs van het model"]
        return call
    details = _get(usage, "input_tokens_details")
    images = _count(details, "image_tokens")
    if images is None:
        _flag(call, "fototokens niet gerapporteerd: alles als tekst geteld")
        images = 0
    text = max(inp - images, 0)
    call.cost_usd = (text * p["input"] + images * p.get("image_input", p["input"]) + out * p["output"]) / 1_000_000
    return call


def track_openai_image(requested: str, response) -> None:
    try:
        record(from_openai_image(requested, getattr(response, "usage", None)))
    except Exception as e:
        print(f"cost of an OpenAI picture: {e!r}")
        record(Call("openai", requested, status="unknown", missing=["kost kon niet berekend worden"]))


def lost(provider: str, model: str, reason: str) -> None:
    """A call that was sent but never answered (cut off after too long): it may be billed, its usage is unknown."""
    record(Call(provider, model, status="unknown", missing=[reason]))


def _status(statuses) -> str:
    statuses = list(statuses)
    if statuses and all(s == "estimated" for s in statuses):
        return "estimated"
    return "unknown" if all(s == "unknown" for s in statuses) else "partial"


def summary(calls: list[Call]) -> dict:
    """The analysis as one: all its calls added up."""
    total = lambda key: sum(getattr(c, key) for c in calls)
    usd = total("cost_usd")
    return {
        "input_tokens": total("input_tokens"), "output_tokens": total("output_tokens"),
        "cached_input_tokens": total("cached_input_tokens"), "cache_write_tokens": total("cache_write_tokens"),
        "cost_usd": round(usd, 8), "cost_eur": round(usd * USD_TO_EUR, 6),
        "status": _status(c.status for c in calls),
        "models": ", ".join(dict.fromkeys(c.model for c in calls)),
    }


COLUMNS = ("id", "at", "task", "status", "failed", "models", "input_tokens", "output_tokens", "cached_input_tokens",
           "cache_write_tokens", "cost_usd", "cost_eur")


def store(user: dict, task: str, calls: list[Call], *, failed: bool = False) -> dict | None:
    """Keeps one analysis with its calls; nothing when it made no API call. Never raises."""
    if not calls:
        return None
    row = {"id": db.new_id(), "at": db.now_iso(), "task": task, "failed": int(failed), **summary(calls)}
    try:
        with db.connect() as conn:
            conn.execute(f"INSERT INTO ai_usage (user_id, calls, {', '.join(COLUMNS)}) VALUES (?, ?, {', '.join('?' * len(COLUMNS))})",
                         (user["id"], json.dumps([asdict(c) for c in calls]), *(row[k] for k in COLUMNS)))
    except Exception as e:
        print(f"keeping the cost of {task} failed: {e!r}")
    return row


def overview(conn, user: dict) -> dict:
    """This year's analyses for Instellingen: the total, per model, and the latest ones. Starts again on 1 January
    (local time); earlier years stay in the database."""
    year = datetime.now().astimezone()
    start = year.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
    rows = [dict(r) for r in conn.execute(f"SELECT {', '.join(COLUMNS)} FROM ai_usage WHERE user_id = ? AND at >= ? ORDER BY at DESC",
                                          (user["id"], start))]
    per_model: dict[str, dict] = {}
    for r in rows:
        m = per_model.setdefault(r["models"], {"models": r["models"], "count": 0, "cost_eur": 0.0, "partial": False})
        m["count"] += 1
        m["cost_eur"] += r["cost_eur"] or 0
        m["partial"] |= r["status"] != "estimated"
    return {"year": year.year, "count": len(rows), "cost_eur": round(sum(r["cost_eur"] or 0 for r in rows), 6),
            "partial": any(r["status"] != "estimated" for r in rows),
            "per_model": sorted(per_model.values(), key=lambda m: -m["cost_eur"]), "latest": rows[:10]}


_overrides()
