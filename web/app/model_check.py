"""Which OpenAI models really do what Dressing asks of them, tested against the live API.

Dressing asks four things of a ChatGPT model (gpt.run_until_tool_call): read a photo (input_image), answer by calling
a function, take a reasoning effort (low for everyday work, medium for a dress code) and accept cache breakpoints
(prompt_cache_options and prompt_cache_breakpoint) with usage that reports cached and written tokens. check() asks
one model all of that in two small requests, the way the app does; the result per model is kept in
DATA_DIR/openai_models.json. A model is recommended in the model list (Instellingen → Ander model kiezen) only when
the last check passed all four. Nothing is assumed from a model's name or the documentation.

Run it with an OpenAI key, from the web folder (or in the container: docker compose exec dressing python -m ...):
    OPENAI_API_KEY=sk-... python -m app.model_check                 # the models the app routes to
    OPENAI_API_KEY=sk-... python -m app.model_check gpt-6-sol       # or the ones named
Both requests together cost a fraction of a cent on Luna and a few cents on Astra; the check prints its cost.
"""

import asyncio
import base64
import io
import json
import os
import sys
from datetime import date

import openai

from . import costs, db

FILE = db.DATA_DIR / "openai_models.json"
FEATURES = {"image": "foto lezen", "function": "functie aanroepen", "reasoning": "reasoning-instelling",
            "cache": "prompt caching"}
CACHED = {"mode": "explicit"}
COLOR_TOOL = {"type": "function", "name": "geef_kleur", "strict": False,
              "description": "Geef de hoofdkleur van de foto.",
              "parameters": {"type": "object", "properties": {"kleur": {"type": "string", "description": "De kleur, in het Nederlands."}},
                             "required": ["kleur"]}}
# Long enough to be cached (OpenAI caches from 1024 tokens on), fixed so a second request can read it back.
PREFIX = "Je bent de herkenning van een kleerkast-app. " + " ".join(
    f"Regel {n}: beschrijf enkel wat op de foto te zien is, in het Nederlands, kort en precies." for n in range(1, 121))
BLUE = {"blauw", "marine", "navy", "donkerblauw", "marineblauw"}


def _photo() -> str:
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (64, 64), "navy").save(out, "JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()


def results() -> dict:
    """The last check of each model, by id."""
    try:
        data = json.loads(FILE.read_text()) if FILE.exists() else {}
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def passed(result: dict | None) -> bool:
    return bool(result) and all(result.get(f) is True for f in FEATURES)


def recommended() -> set[str]:
    return {m for m, r in results().items() if passed(r)}


def _keep(model: str, result: dict) -> None:
    data = results()
    data[model] = result
    FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def _rejected(e: openai.BadRequestError, kwargs: dict, result: dict) -> bool:
    """Marks what a 400 says the model doesn't take and drops it from the request; False when it names nothing."""
    text = str(e.message).lower()
    if "reasoning" in text and "reasoning" in kwargs:
        result["reasoning"] = False
        kwargs.pop("reasoning")
    elif "prompt_cache" in text and "prompt_cache_options" in kwargs:
        result["cache"] = False
        kwargs.pop("prompt_cache_options")
        kwargs["input"] = [{**m, "content": [{k: v for k, v in p.items() if k != "prompt_cache_breakpoint"} for p in m["content"]]}
                           for m in kwargs["input"]]
    elif "image" in text and any(p.get("type") == "input_image" for m in kwargs["input"] for p in m["content"]):
        result["image"] = False
        kwargs["input"] = [{**m, "content": [p for p in m["content"] if p.get("type") != "input_image"]} for m in kwargs["input"]]
    else:
        return False
    return True


async def _ask(client, kwargs: dict, result: dict, calls: list):
    for _ in range(4):
        try:
            response = await client.responses.create(**kwargs)
        except openai.BadRequestError as e:
            if _rejected(e, kwargs, result):
                continue
            result["error"] = str(e.message)[:300]
            return None
        calls.append(costs.from_openai(getattr(response, "model", None) or kwargs["model"], getattr(response, "usage", None),
                                       getattr(response, "service_tier", None)))
        return response
    return None


def _color(response) -> str | None:
    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", None) == "function_call" and item.name == COLOR_TOOL["name"]:
            try:
                return str(json.loads(item.arguments or "{}").get("kleur") or "")
            except (ValueError, AttributeError):
                return ""
    return None


async def check(client, model: str) -> dict:
    """Two requests as the app makes them. The first: the fixed prefix with a breakpoint, a photo, the function and
    effort low. The second: the same prefix, effort medium, the function required; its usage should read the prefix
    from the cache. Each feature is True, False, or None when it couldn't be tried."""
    result: dict = {"checked": date.today().isoformat(), **{f: None for f in FEATURES}, "error": None, "notes": []}
    calls: list[costs.Call] = []
    fixed = {"role": "developer", "content": [{"type": "input_text", "text": PREFIX, "prompt_cache_breakpoint": CACHED}]}
    common = {"model": model, "tools": [COLOR_TOOL], "max_output_tokens": 2000, "prompt_cache_options": CACHED,
              "prompt_cache_key": "dressing-model-check"}
    first = {**common, "reasoning": {"effort": "low"}, "tool_choice": "auto", "input": [fixed, {"role": "user", "content": [
        {"type": "input_image", "image_url": _photo(), "detail": "auto"},
        {"type": "input_text", "text": "Welke kleur heeft deze foto? Roep geef_kleur aan."}]}]}
    response = await _ask(client, first, result, calls)
    if response is not None:
        color = _color(response)
        result["function"] = color is not None
        if result["image"] is None:
            # Accepted, and seen: the answer names the colour of the photo (a navy square).
            result["image"] = bool(color) and any(word in color.lower() for word in BLUE)
            if color is not None and not result["image"]:
                result["notes"].append(f"foto gelezen als '{color}'")
        if result["reasoning"] is None:
            result["reasoning"] = "reasoning" in first
    second = {**common, "reasoning": {"effort": "medium"}, "tool_choice": {"type": "function", "name": COLOR_TOOL["name"]},
              "input": [fixed, {"role": "user", "content": [{"type": "input_text", "text": "Roep geef_kleur aan met 'blauw'."}]}]}
    if result["reasoning"] is False:
        second.pop("reasoning")
    if result["cache"] is False:
        second = {k: v for k, v in second.items() if k != "prompt_cache_options"}
        second["input"] = [{**m, "content": [{k: v for k, v in p.items() if k != "prompt_cache_breakpoint"} for p in m["content"]]}
                           for m in second["input"]]
    again = await _ask(client, second, result, calls)
    if again is not None:
        if result["reasoning"] is not False and "reasoning" not in second:
            result["reasoning"] = False
        if result["function"] is not False:
            result["function"] = _color(again) is not None
        if result["cache"] is None:
            details = getattr(getattr(again, "usage", None), "input_tokens_details", None)
            cached = getattr(details, "cached_tokens", None)
            written = getattr(calls[0], "cache_write_tokens", 0) if calls else 0
            # Accepted and reported: what Dressing needs to price it. A read is not guaranteed (OpenAI routes a request
            # to a cache on a best-effort basis), so a miss is noted, not failed.
            result["cache"] = cached is not None and getattr(details, "cache_write_tokens", None) is not None
            if result["cache"] and not cached:
                result["notes"].append(f"geen cache-treffer bij de tweede vraag ({written} tokens geschreven bij de eerste)")
    result["cost_usd"] = round(sum(c.cost_usd for c in calls), 6)
    result["cost_status"] = costs._status(c.status for c in calls) if calls else "unknown"
    return result


async def check_all(key: str, models: list[str]) -> dict:
    async with openai.AsyncOpenAI(api_key=key, timeout=120.0, max_retries=1) as client:
        out = {}
        for model in models:
            try:
                out[model] = await check(client, model)
            except openai.OpenAIError as e:
                out[model] = {"checked": date.today().isoformat(), **{f: None for f in FEATURES}, "error": f"{type(e).__name__}: {e}"[:300],
                              "notes": []}
            _keep(model, out[model])
        return out


def main(argv: list[str]) -> int:
    from . import gpt
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        print("Zet OPENAI_API_KEY om de modellen te testen.")
        return 2
    models = argv or list(dict.fromkeys([gpt.DEFAULT_MODEL, gpt.HELPER_MODEL, gpt.COMPLEX_MODEL, gpt.REVIEW_MODEL]))
    db.DATA_DIR.mkdir(parents=True, exist_ok=True)
    for model, r in asyncio.run(check_all(key, models)).items():
        marks = ", ".join(f"{label}: {'ja' if r.get(f) else 'nee' if r.get(f) is False else '?'}" for f, label in FEATURES.items())
        print(f"{model}: {'GESLAAGD' if passed(r) else 'NIET GESLAAGD'} ({marks})"
              + (f" · ${r['cost_usd']:.4f}" if "cost_usd" in r else "")
              + (f" · fout: {r['error']}" if r.get("error") else "") + "".join(f" · {n}" for n in r.get("notes", [])))
    print(f"Bewaard in {FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
