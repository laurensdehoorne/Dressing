"""Recognising a piece of clothing from a photo, and the stylist that puts outfits together from the dressing:
the questions, tools and answers, and the Claude API calls. gpt.py asks the same through OpenAI (ChatGPT)."""

import asyncio
import base64
import contextvars
import os
from collections.abc import Awaitable, Callable
from datetime import date

import anthropic

from . import auth, costs, db, keys

DEFAULT_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
HELPER_MODEL = os.environ.get("CLAUDE_HELPER_MODEL", "claude-haiku-5-5")  # the cheap model of "Zuinig"
# Zuinig: recognising a piece and reading an order with the cheap helper model; the stylist keeps the account's model.
QUALITY = {"zuinig": "Zuinig", "standaard": "Standaard"}
DEFAULT_QUALITY = "standaard"
# One analysis never takes longer than this: past it, the app says so instead of showing its spinner for minutes.
LOOKUP_SECONDS = float(os.environ.get("LOOKUP_SECONDS", "180"))
TOO_SLOW = "Het antwoord duurde te lang. Probeer opnieuw."

# The progress of the analysis running in this task: a background job's, which the app shows while it waits.
PROGRESS: contextvars.ContextVar[dict | None] = contextvars.ContextVar("progress", default=None)

_clients: dict[str, anthropic.AsyncAnthropic] = {}
_no_fallbacks: set[str] = set()  # models that rejected the server-side `fallbacks` parameter


class ClaudeError(Exception):
    """Error with a Dutch message that can be shown to the user as-is."""


def report(**changes) -> None:
    if (progress := PROGRESS.get()) is not None:
        progress.update(changes)


def _reason(e: anthropic.APIStatusError) -> str:
    """Anthropic's own explanation from the error body, not the SDK's 'Error code: 400 - {...}' dump."""
    body = e.body if isinstance(e.body, dict) else {}
    message = (body.get("error") or {}).get("message") if isinstance(body.get("error"), dict) else None
    return f"Fout {e.status_code} van Anthropic: {message}" if message else f"Fout {e.status_code} van Anthropic."


def client(user: dict) -> anthropic.AsyncAnthropic:
    key, source = keys.active_key(user, "claude")
    if source == "locked":
        raise ClaudeError("Je API-sleutel is vergrendeld op dit toestel. Log uit en opnieuw in om hem te ontgrendelen.")
    if not key:
        raise ClaudeError("Geen API-sleutel ingesteld voor je account. Voeg er een toe bij Instellingen.")
    if key not in _clients:
        _clients[key] = anthropic.AsyncAnthropic(api_key=key, timeout=150.0, max_retries=1)
    return _clients[key]


def model(user: dict) -> str:
    """The model this account chose, else CLAUDE_MODEL from .env, else the built-in default."""
    return auth.user_settings(user).get("model") or DEFAULT_MODEL


def quality(user: dict) -> str:
    chosen = auth.user_settings(user).get("quality")
    return chosen if chosen in QUALITY else DEFAULT_QUALITY


def thrifty(user: dict) -> bool:
    """Whether this account's lighter work (a photo, an order) goes to the cheap helper model."""
    return quality(user) == "zuinig"


def _sees_photos(m) -> bool:
    caps = m.capabilities
    node = caps.get("image_input") if isinstance(caps, dict) else getattr(caps, "image_input", None)
    return bool(node.get("supported") if isinstance(node, dict) else getattr(node, "supported", None))


async def list_models(user: dict) -> list[dict]:
    """Models this key can use that can read photos, newest first. Listing models is free."""
    try:
        models = [m async for m in client(user).models.list() if _sees_photos(m)]
    except anthropic.AuthenticationError:
        raise ClaudeError("De API-sleutel is ongeldig. Pas hem aan bij Instellingen.")
    except anthropic.APIConnectionError:
        raise ClaudeError("De server kan Anthropic niet bereiken.")
    except anthropic.APIStatusError as e:
        raise ClaudeError(f"Modellen ophalen mislukt (fout {e.status_code}).")
    models.sort(key=lambda m: m.created_at, reverse=True)
    return [{"id": m.id, "name": m.display_name} for m in models]


async def check_key(key: str) -> None:
    """Raises ClaudeError when Anthropic rejects the key. Listing models is free."""
    try:
        async with anthropic.AsyncAnthropic(api_key=key, timeout=20.0, max_retries=1) as probe:
            await probe.models.list(limit=1)
    except anthropic.AuthenticationError:
        raise ClaudeError("Deze API-sleutel is ongeldig of ingetrokken.")
    except anthropic.PermissionDeniedError:
        raise ClaudeError("Deze API-sleutel heeft geen toegang tot de Claude API.")
    except anthropic.APIConnectionError:
        raise ClaudeError("De server kan Anthropic niet bereiken om de sleutel te controleren.")
    except anthropic.APIStatusError as e:
        raise ClaudeError(f"Controle mislukt (fout {e.status_code}).")


async def _create(user: dict, **kwargs):
    """messages.create with server-side refusal fallbacks, dropped for models that don't accept them."""
    name = kwargs["model"]
    if name not in _no_fallbacks and not name.startswith("claude-haiku"):  # Haiku has no server-side fallback
        try:
            return await client(user).beta.messages.create(**kwargs, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        except anthropic.BadRequestError as e:
            if "fallback" not in str(e.message).lower():
                raise
            _no_fallbacks.add(name)
    return await client(user).beta.messages.create(**kwargs)


# Tools the AI may call on the way to its answer, by name: the tool and what answers it (its input in, text out).
Helpers = dict[str, tuple[dict, Callable[[dict], Awaitable[str]]]]


async def run_helper(helpers: Helpers, name: str, given) -> str:
    try:
        return await helpers[name][1](given if isinstance(given, dict) else {})
    except Exception as e:  # the answer goes on without it
        return f"Niet gelukt: {e}"


async def _ask(user: dict, tool: dict, *, system: str, content: list, effort: str = "low", helpers: Helpers | None = None,
               helper: bool = False) -> dict:
    """Asks the AI service this account chose; the questions, tool and answers are the same for both. With `helper`,
    its cheap helper model instead of the account's model."""
    if keys.provider(user) == "openai":
        from . import gpt
        name, provider, asking = (gpt.HELPER_MODEL if helper else gpt.model(user)), "openai", gpt.run_until_tool_call
    else:
        name, provider, asking = (HELPER_MODEL if helper else model(user)), "anthropic", _ask_claude
    try:
        return await asking(user, tool, system=system, content=content, effort=effort, helpers=helpers or {}, name=name)
    except asyncio.CancelledError:  # cut off after too long (_timed): the call may still be billed
        costs.lost(provider, name, "afgebroken na te lang wachten")
        raise


async def _ask_claude(user: dict, tool: dict, *, system: str, content: list, effort: str, helpers: Helpers, name: str) -> dict:
    """Sends the question to model `name` until Claude calls `tool`, then returns that call's input; a helper it calls
    on the way is answered and the conversation goes on. Forced tool use isn't accepted by the current models, so the
    tool is offered and asked for by name."""
    messages: list[dict] = [{"role": "user", "content": content}]
    for _ in range(3 + 2 * bool(helpers)):
        try:
            response = await _create(
                user,
                model=name,
                max_tokens=16000,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=messages,
                tools=[tool, *(t for t, _ in helpers.values())],
                tool_choice={"type": "auto"},
                output_config={"effort": effort},
            )
        except anthropic.AuthenticationError:
            raise ClaudeError("De API-sleutel is ongeldig. Pas hem aan bij Instellingen.")
        except anthropic.RateLimitError:
            raise ClaudeError("Te veel verzoeken tegelijk. Wacht even en probeer opnieuw.")
        except anthropic.APITimeoutError:
            raise ClaudeError(TOO_SLOW)
        except anthropic.APIConnectionError:
            raise ClaudeError("De server kan Claude niet bereiken. Controleer de internetverbinding.")
        except anthropic.APIStatusError as e:
            if e.status_code >= 500:
                raise ClaudeError("Claude is tijdelijk overbelast. Probeer zo dadelijk opnieuw.")
            raise ClaudeError(_reason(e))
        costs.track_anthropic(name, response)

        for block in response.content:
            if block.type == "tool_use" and block.name == tool["name"]:
                return dict(block.input) if isinstance(block.input, dict) else {}
        if calls := [b for b in response.content if b.type == "tool_use" and b.name in helpers]:
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": b.id, "content": await run_helper(helpers, b.name, b.input)} for b in calls]})
            continue
        if response.stop_reason == "refusal":
            raise ClaudeError("Claude weigerde dit verzoek. Probeer het opnieuw met een andere foto of vraag.")
        if response.stop_reason == "max_tokens":
            raise ClaudeError("Het antwoord werd afgebroken (te lang). Probeer opnieuw.")
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": f"Roep nu de tool {tool['name']} aan met je resultaat."})
    raise ClaudeError("Geen resultaat ontvangen van Claude. Probeer opnieuw.")


async def _timed(asking) -> dict:
    try:
        return await asyncio.wait_for(asking, LOOKUP_SECONDS)
    except asyncio.TimeoutError:
        raise ClaudeError(TOO_SLOW)


# MARK: - Recognising a piece from a photo

async def identify_item(user: dict, photo_jpeg: bytes | None, hint: str | None) -> dict:
    """What a photographed piece of clothing is and how it looks, as the fields of a new piece."""
    content: list = []
    if photo_jpeg:
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": base64.b64encode(photo_jpeg).decode()}})
    content.append({"type": "text", "text": f"Omschrijving van de gebruiker: {hint}" if hint else "Beschrijf dit kledingstuk."})
    report(step="Foto bekijken")
    got = await _timed(_ask(user, ITEM_TOOL, system=IDENTIFY_SYSTEM, content=content, helper=thrifty(user)))
    info = db.clean_item({
        "name": got.get("naam"), "category": got.get("categorie"), "kind": got.get("soort"), "colors": got.get("kleuren"),
        "pattern": got.get("patroon"), "material": got.get("materiaal"), "brand": got.get("merk"), "fit": got.get("pasvorm"),
        "seasons": got.get("seizoenen"), "warmth": got.get("warmte"), "formality": got.get("formaliteit"),
        "occasions": got.get("gelegenheden"), "style_tags": got.get("stijl"), "description": got.get("beschrijving"),
        "care": got.get("onderhoud"),
    })
    return {k: v for k, v in info.items() if v not in (None, "", [])}


# MARK: - Reading an order confirmation

def _order_piece(got: dict, shop: str, day: str) -> dict:
    info = db.clean_item({
        "name": got.get("naam"), "category": got.get("categorie"), "kind": got.get("soort"), "colors": got.get("kleuren"),
        "pattern": got.get("patroon"), "material": got.get("materiaal"), "brand": got.get("merk") or shop,
        "size": got.get("maat"), "fit": got.get("pasvorm"), "seasons": got.get("seizoenen"), "warmth": got.get("warmte"),
        "formality": got.get("formaliteit"), "occasions": got.get("gelegenheden"), "style_tags": got.get("stijl"),
        "description": got.get("beschrijving"), "price": got.get("prijs"), "purchase_date": day,
    })
    return {k: v for k, v in info.items() if v not in (None, "", [])}


def _image(jpeg: bytes) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(jpeg).decode()}}


def _box(value, size: tuple[int, int]) -> list[int] | None:
    """A rectangle [left, top, right, bottom] inside a screenshot of this size, big enough to be a product photo."""
    try:
        x0, y0, x1, y1 = (int(round(float(v))) for v in value)
    except (TypeError, ValueError):
        return None
    w, h = size
    x0, x1 = sorted((max(0, min(w, x0)), max(0, min(w, x1))))
    y0, y1 = sorted((max(0, min(h, y0)), max(0, min(h, y1))))
    return [x0, y0, x1, y1] if x1 - x0 >= 30 and y1 - y0 >= 30 else None


async def read_order(user: dict, text: str, photos: dict[int, bytes], shots: list[tuple[bytes, tuple[int, int]]] = ()) -> dict:
    """The pieces of clothing in an order confirmation (Zara, H&M, Mango, ...), each with the number of its photo when it
    has one. The AI sees the pictures (small JPEGs by number): to match them to the pieces, and for the colour when the
    mail only shows a swatch. From screenshots (JPEG and its size) it reads everything, and says where each piece's
    photo is on them so it can be cut out."""
    content: list = []
    for n, jpeg in photos.items():
        content += [{"type": "text", "text": f"[foto {n}]"}, _image(jpeg)]
    for n, (jpeg, (w, h)) in enumerate(shots, 1):
        content += [{"type": "text", "text": f"[screenshot {n}: {w}×{h} pixels]"}, _image(jpeg)]
    question = f"Vandaag is het {date.today().isoformat()}."
    question += f"\n\nDe bestelmail:\n{text}" if text else "\n\nLees de bestelling van de screenshots hierboven."
    content.append({"type": "text", "text": question})
    report(step="Bestelling lezen")
    got = await _timed(_ask(user, ORDER_TOOL, system=ORDER_SYSTEM, content=content, helper=thrifty(user)))
    shop = str(got.get("winkel") or "").strip()[:80]
    day = str(got.get("besteldatum") or "").strip()[:10]
    try:
        date.fromisoformat(day)
    except ValueError:
        day = ""
    pieces = []
    for p in got.get("stukken") or []:
        if not isinstance(p, dict) or not p.get("naam"):
            continue
        try:
            photo = int(p.get("foto") or 0)
        except (TypeError, ValueError):
            photo = 0
        piece = {"info": _order_piece(p, shop, day), "photo": photo if photo in photos else 0}
        try:
            shot = int(p.get("screenshot") or 0)
        except (TypeError, ValueError):
            shot = 0
        if 1 <= shot <= len(shots) and (box := _box(p.get("vak"), shots[shot - 1][1])):
            piece["crop"] = (shot, box)
        pieces.append(piece)
    return {"shop": shop, "date": day, "pieces": pieces}


# MARK: - Inspiration: outfits the user likes, and the style they add up to

MAX_LOOKS = 60  # the descriptions of earlier looks that go along to write the profile; the newest count


def _look(got: dict, shots: list[tuple[bytes, tuple[int, int]]]) -> dict | None:
    try:
        n = int(got.get("foto") or 0)
    except (TypeError, ValueError):
        return None
    if not 1 <= n <= len(shots) or got.get("geen_outfit") or not str(got.get("beschrijving") or "").strip():
        return None
    return {"photo": n, "box": _box(got.get("vak"), shots[n - 1][1]),
            "description": str(got["beschrijving"]).strip()[:600],
            "colors": db._words(got.get("kleuren"), 8), "style_tags": db._words(got.get("stijl"), 8),
            "pieces": db._words(got.get("stukken"), 10)}


async def read_inspiration(user: dict, shots: list[tuple[bytes, tuple[int, int]]], looks: list[str], profile: str) -> dict:
    """What each picture of an outfit the user likes shows (and where on a screenshot the outfit is, to cut it out),
    and the style profile written anew from these and the earlier looks. Without pictures only the profile."""
    content: list = []
    for n, (jpeg, (w, h)) in enumerate(shots, 1):
        content += [{"type": "text", "text": f"[foto {n}: {w}×{h} pixels]"}, _image(jpeg)]
    question = (f"Mijn stijlprofiel tot nu toe:\n{profile}" if profile else "Er is nog geen stijlprofiel.")
    if looks:
        question += "\n\nOutfits die ik eerder bewaarde:\n" + "\n".join(f"- {look}" for look in looks[-MAX_LOOKS:])
    question += ("\n\nBeschrijf de outfits op de nieuwe foto's hierboven en schrijf het stijlprofiel opnieuw." if shots
                 else "\n\nGeen nieuwe foto's: schrijf het stijlprofiel opnieuw uit de outfits hierboven; looks is leeg.")
    content.append({"type": "text", "text": question})
    report(step="Outfits bekijken" if shots else "Stijlprofiel opmaken")
    got = await _timed(_ask(user, INSPIRATION_TOOL, system=INSPIRATION_SYSTEM, content=content, helper=thrifty(user)))
    found = [look for g in got.get("looks") or [] if isinstance(g, dict) and (look := _look(g, shots))]
    return {"looks": found, "profile": str(got.get("profiel") or "").strip()[:1500]}


# MARK: - The stylist

REJECTED_SHOWN = 25  # the latest turned-down outfits the stylist sees
_SEASON_OF_MONTH = {12: "winter", 1: "winter", 2: "winter", 3: "lente", 4: "lente", 5: "lente",
                    6: "zomer", 7: "zomer", 8: "zomer", 9: "herfst", 10: "herfst", 11: "herfst"}


def season(day: date) -> str:
    """The season of a day as people dress for it here: by month, December to February is winter."""
    return _SEASON_OF_MONTH[day.month]

def _rejected_line(r: dict, names: dict[str, str]) -> str:
    line = f"{r['at'][:10]}: " + " + ".join(names.get(i, "?") for i in r.get("item_ids", []))
    if r.get("query"):
        line += f" (voor '{r['query'][:80]}')"
    why = [*r.get("reasons", []), *([f"'{r['note'][:120]}'"] if r.get("note") else [])]
    return line + (": " + ", ".join(why) if why else "")


def _item_line(item: dict, wear: dict) -> str:
    """One piece for the stylist: only what it needs to choose, to keep the question small."""
    parts = [item["id"], db.CATEGORIES.get(item.get("category"), "?")]
    parts += [item.get(k) for k in ("kind", "name") if item.get(k)]
    if item.get("colors"):
        parts.append("kleur " + "/".join(item["colors"]))
    for key in ("pattern", "material", "fit"):
        if item.get(key):
            parts.append(item[key])
    if item.get("warmth"):
        parts.append(f"warmte {item['warmth']}/5")
    if item.get("formality"):
        parts.append(f"formeel {item['formality']}/5")
    if item.get("seasons"):
        parts.append("seizoen " + "/".join(item["seasons"]))
    if item.get("style_tags"):
        parts.append("stijl " + ", ".join(item["style_tags"][:5]))
    if item.get("favorite"):
        parts.append("favoriet")
    if w := wear.get(item["id"]):
        parts.append(f"{w['count']}× gedragen, laatst {w['last']}")
    else:
        parts.append("nog nooit gedragen")
    return " | ".join(str(p) for p in parts)


async def suggest_outfits(user: dict, query: str, items: list[dict], worn: list[dict], *, weather: str = "",
                          profile: str = "", style: str = "", packed: list[str] | None = None,
                          weather_at: Callable[[dict], Awaitable[str]] | None = None, trip: bool = False, rejected: list[dict] = (),
                          previous: dict | None = None) -> dict:
    """1 to 3 outfits from the pieces in the dressing for this question (an occasion, the weather, a mood); on a trip
    (packed: what is in the suitcase) only from the suitcase. style: the profile the AI wrote from outfits the user
    likes (Inspiratie). trip: on a trip (also while packing), where only the destination's weather counts, not the
    season. rejected: outfits the user turned down, and why. previous: for a follow-up ("andere schoenen"),
    what was asked before and what was suggested, to adjust rather than start over. With weather_at the stylist can look up the weather of
    another place or a later day the question is about (an outing to Amsterdam, Saturday)."""
    wear: dict[str, dict] = {}
    for log in sorted(worn, key=lambda l: l["date"]):
        for item_id in log.get("item_ids", []):
            w = wear.setdefault(item_id, {"count": 0, "last": ""})
            w["count"] += 1
            w["last"] = log["date"][:10]
    if packed is not None:
        available = [i for i in items if i["id"] in packed]
        if not available:
            raise ClaudeError("Je valies is nog leeg. Leg er eerst kleren in.")
    else:
        available = [i for i in items if (i.get("status") or "dressing") == "dressing"]
        if not available:
            raise ClaudeError("Er hangt nog niets in je dressing (of alles is in de was). Voeg eerst kledingstukken toe.")
    names = {i["id"]: i.get("name") or i.get("kind") or "" for i in items}
    taste = []
    for log in sorted(worn, key=lambda l: l["date"], reverse=True)[:25]:
        if not (log.get("rating") or log.get("note") or log.get("occasion")):
            continue
        line = f"{log['date'][:10]}: " + " + ".join(names.get(i, "?") for i in log.get("item_ids", []))
        if log.get("occasion"):
            line += f" ({log['occasion']})"
        if log.get("rating"):
            line += f", score {log['rating']}/5"
        if log.get("note"):
            line += f", '{log['note'][:120]}'"
        taste.append(line)
    where = "Mijn valies: ik ben op reis en heb enkel dit bij me" if packed is not None else "Mijn dressing"
    wardrobe = f"{where} (één stuk per regel, id vooraan):\n" + "\n".join(_item_line(i, wear) for i in available)
    if style:
        wardrobe = f"Mijn stijl, uit outfits die ik mooi vind:\n{style}\n\n{wardrobe}"
    if profile:
        wardrobe = f"Over mij: {profile}\n\n{wardrobe}"
    if taste:
        wardrobe += "\n\nWat ik eerder droeg en hoe het viel:\n" + "\n".join(taste)
    if refused := [_rejected_line(r, names) for r in rejected[:REJECTED_SHOWN]]:
        wardrobe += "\n\nOutfits die je voorstelde en die ik afwees, met waarom:\n" + "\n".join(refused)
    question = f"Vandaag is het {date.today().isoformat()}." if trip else f"Vandaag is het {date.today().isoformat()} ({season(date.today())})."
    if weather:
        question += f"\n{weather}"
    if trip:
        question += "\nOp reis telt enkel het weer op mijn bestemming, niet het seizoen."
    helpers: Helpers = {WEATHER_TOOL["name"]: (WEATHER_TOOL, weather_at)} if weather_at else {}
    if helpers:
        question += ("\nGaat de vraag over een andere plaats dan hierboven (een uitstap, een reis, een feest elders) of over "
                     "een dag na morgen, vraag dan eerst het weer van die plaats en die dag op met weer_opvragen en kies naar dat weer.")
    if previous:
        asked = " → ".join(previous.get("queries") or [])
        shown = []
        for n, o in enumerate(previous.get("outfits") or [], 1):
            pieces = ", ".join(f"{i} ({names[i]})" for i in o.get("item_ids", []) if i in names)
            shown.append(f"Outfit {n} '{o.get('title') or ''}': {pieces}")
        question += f"\nEerder vroeg ik: {asked}\nJe stelde voor:\n" + "\n".join(shown)
        question += ("\nIk stuur je voorstel bij. Geef alle outfits uit je voorstel terug, in dezelfde volgorde, en pas in elk "
                     "enkel aan wat ik vraag (gaat het over één outfit, bv. 'outfit 2', dan enkel dat). Houd wat ik niet vernoem; "
                     "de eerdere vraag (gelegenheid, weer) blijft gelden.")
        question += f"\nBijsturing: {query}"
    else:
        question += f"\nVraag: {query}"
    # The dressing first, cached on its own: a next question within a few minutes only pays for the question.
    content = [{"type": "text", "text": wardrobe, "cache_control": {"type": "ephemeral"}}, {"type": "text", "text": question}]
    report(step="Outfits samenstellen")
    got = await _timed(_ask(user, OUTFIT_TOOL, system=STYLIST_SYSTEM, content=content, helpers=helpers))
    by_id = {i["id"]: i for i in available}
    outfits, problems = _outfits(got, by_id, len((previous or {}).get("outfits") or []))
    if problems:
        # Asked once more with what is wrong (the dressing is cached, so this costs little); kept when it is better.
        report(step="Outfits nakijken")
        shown = "\n".join(f"Outfit {n} '{o['title']}': " + ", ".join(f"{i} ({names[i]})" for i in o["item_ids"])
                          for n, o in enumerate(outfits, 1)) or "geen bruikbare outfits"
        fix = (f"Je antwoord:\n{shown}\n\nDat klopt niet:\n" + "\n".join(f"- {p}" for p in problems) +
               "\n\nRoep geef_outfits opnieuw aan met alle outfits, verbeterd. Verander verder niets aan wat goed was.")
        again = await _timed(_ask(user, OUTFIT_TOOL, system=STYLIST_SYSTEM, content=[*content, {"type": "text", "text": fix}],
                                  helpers=helpers))
        better, left = _outfits(again, by_id, len((previous or {}).get("outfits") or []))
        if better and len(left) < len(problems):
            got, outfits = again, better
    return {"advice": got.get("advies") or "", "outfits": outfits, "missing": got.get("ontbreekt") or ""}


def _outfits(got: dict, by_id: dict[str, dict], expected: int = 0) -> tuple[list[dict], list[str]]:
    """The outfits in the stylist's answer, with only pieces from the list, and what is wrong with them: an id that
    isn't in the list, no shoes while there are shoes to choose from, fewer outfits than the suggestion it adjusted."""
    has_shoes = any(i.get("category") == "schoenen" for i in by_id.values())
    outfits, problems = [], []
    for o in got.get("outfits") or []:
        if not isinstance(o, dict):
            continue
        given = [str(i) for i in o.get("stukken") or []]
        ids = list(dict.fromkeys(i for i in given if i in by_id))
        title = o.get("titel") or ""
        if unknown := [i for i in given if i not in by_id]:
            problems.append(f"Outfit '{title}' heeft id's die niet in de lijst staan: {', '.join(unknown[:5])}. Gebruik de id's precies zoals in de lijst.")
        if ids and has_shoes and not any(by_id[i].get("category") == "schoenen" for i in ids):
            problems.append(f"Outfit '{title}' heeft geen schoenen: kies er een paar uit de lijst bij.")
        if ids:
            outfits.append({"title": title, "item_ids": ids, "reason": o.get("waarom") or "", "tip": o.get("tip") or ""})
    if expected and len(outfits) < expected:
        problems.append(f"Je voorstel had {expected} outfits en je gaf er {len(outfits)} terug: geef ze alle {expected} terug, "
                        "elk aangepast zoals gevraagd.")
    return outfits, problems


IDENTIFY_SYSTEM = f"""Je helpt iemand de kleren in zijn of haar dressing te registreren. Je krijgt een foto van één kledingstuk (op een hanger, opgevouwen, op de grond of gedragen) en/of een korte omschrijving. Beschrijf het belangrijkste kledingstuk op de foto, zo dat je het later uit een lijst kan herkennen en ermee outfits kan samenstellen.

Roep precies één keer de tool registreer_kledingstuk aan. Regels:
- Schrijf in het Nederlands (Vlaams), kort en concreet.
- categorie is één van: {", ".join(db.CATEGORIES)}.
- naam is een korte, herkenbare naam zoals iemand het zelf zou noemen: "Marineblauwe wollen trui met col", "Lichtblauwe slim jeans", "Witte leren sneakers".
- kleuren: de hoofdkleur eerst, gewone kleurnamen (bv. "marineblauw", "ecru", "camel", "bordeaux").
- merk enkel als je het echt kan lezen op een label of logo; verzin niets.
- warmte en formaliteit van 1 tot 5. Warmte: 1 heel licht (linnen hemdje) tot 5 heel warm (donsjas). Formaliteit: 1 sportief, 2 casual, 3 smart casual, 4 zakelijk, 5 feestelijk of formeel.
- seizoenen uit lente, zomer, herfst, winter: wanneer je het draagt.
- gelegenheden: 2 tot 5 concrete momenten (bv. "kantoor", "weekend", "etentje", "feest", "wandelen", "sport").
- stijl: 2 tot 5 korte stijltermen (bv. "klassiek", "minimalistisch", "sportief", "bohemian", "preppy").
- beschrijving: 1 à 2 zinnen over hoe het eruitziet en waarmee het goed combineert."""

ITEM_TOOL = {
    "name": "registreer_kledingstuk",
    "description": "Registreer het kledingstuk op de foto in de dressing.",
    "input_schema": {
        "type": "object",
        "properties": {
            "naam": {"type": "string", "description": "Korte herkenbare naam"},
            "categorie": {"type": "string", "enum": list(db.CATEGORIES)},
            "soort": {"type": "string", "description": "Wat het precies is, bv. 'chino', 'overhemd', 'cardigan', 'enkellaars'"},
            "kleuren": {"type": "array", "items": {"type": "string"}},
            "patroon": {"type": "string", "description": "Effen, gestreept, geruit, bloemen, ..."},
            "materiaal": {"type": "string", "description": "Zoals het eruitziet of op het label staat: katoen, wol, denim, leer, ..."},
            "merk": {"type": "string"},
            "pasvorm": {"type": "string", "description": "Slim, regular, oversized, ..."},
            "seizoenen": {"type": "array", "items": {"type": "string", "enum": db.SEASONS}},
            "warmte": {"type": "integer", "minimum": 1, "maximum": 5},
            "formaliteit": {"type": "integer", "minimum": 1, "maximum": 5},
            "gelegenheden": {"type": "array", "items": {"type": "string"}},
            "stijl": {"type": "array", "items": {"type": "string"}},
            "beschrijving": {"type": "string"},
            "onderhoud": {"type": "string", "description": "Enkel als het label leesbaar is: wastemperatuur, niet in de droogkast, ..."},
        },
        "required": ["naam", "categorie", "kleuren"],
    },
}

ORDER_SYSTEM = f"""Je helpt iemand de kleren die hij of zij online kocht in zijn of haar dressing te zetten. Je krijgt de tekst van een bestelbevestiging of verzendmail van een webwinkel (Zara, H&M, Mango, Zalando, COS, ...) en de afbeeldingen uit die mail, elk met zijn nummer [foto N]. Waar een afbeelding in de tekst stond, staat ook [foto N]; bij een PDF staan de afbeeldingen enkel vooraan, in de volgorde van de mail.

Roep precies één keer de tool registreer_bestelling aan met elk gekocht kledingstuk, schoen of accessoire. Regels:
- Sla over wat geen kleding is (cadeaubon, verzendkosten, draagtas, cosmetica, interieur) en wat geannuleerd of teruggestuurd werd. Een stuk dat meermaals besteld werd, staat er één keer in.
- foto: het nummer N van de productfoto van dat stuk. Kijk naar de afbeeldingen zelf en naar waar ze in de mail staan. Een logo, banner, icoon of klein kleurstaaltje is geen productfoto; geen productfoto voor dat stuk: dan 0.
- Krijg je screenshots (van een mail, de app of de website van de winkel), lees dan alles af van de screenshots. Zet bij elk stuk screenshot (het nummer) en vak: de rechthoek rond de productfoto van dat stuk als [links, boven, rechts, onder] in pixels van die screenshot, zo nauw mogelijk rond de foto zelf, zonder tekst, knoppen of rand. Staat hetzelfde stuk op twee screenshots, kies de grootste foto. Geen productfoto te zien: laat vak weg. foto is dan 0.
- kleuren: staat de kleur niet als woord in de mail (enkel als staaltje of bolletje), lees ze dan af van het staaltje of de productfoto.
- winkel: de naam van de webwinkel; merk enkel als het stuk van een ander merk is dan de winkel (bv. bij Zalando).
- besteldatum als JJJJ-MM-DD, als die in de mail staat.
- Neem naam, kleur, maat, prijs (het betaalde bedrag per stuk, als getal) en materiaal over uit de mail; vul de rest aan zoals je dit soort stuk kent, maar verzin geen details die je niet kan weten.
- Schrijf in het Nederlands (Vlaams). naam is een korte, herkenbare naam zoals iemand het zelf zou noemen ("Ecru gebreide trui met ronde hals"), niet de productcode of de Engelse winkelnaam; kleuren met gewone Nederlandse kleurnamen, de hoofdkleur eerst.
- categorie is één van: {", ".join(db.CATEGORIES)}. warmte en formaliteit van 1 tot 5 zoals bij het registreren van één stuk (warmte 1 heel licht tot 5 heel warm; formaliteit 1 sportief tot 5 feestelijk)."""

_ORDER_PIECE = {**ITEM_TOOL["input_schema"]["properties"],
                "maat": {"type": "string"}, "prijs": {"type": "number", "description": "Betaald bedrag per stuk in euro"},
                "foto": {"type": "integer", "description": "Nummer N van de bijhorende [foto N], of 0"},
                "screenshot": {"type": "integer", "description": "Enkel bij screenshots: het nummer van de screenshot met de productfoto"},
                "vak": {"type": "array", "items": {"type": "integer"}, "minItems": 4, "maxItems": 4,
                        "description": "Enkel bij screenshots: [links, boven, rechts, onder] van de productfoto, in pixels"}}
_ORDER_PIECE.pop("onderhoud")

ORDER_TOOL = {
    "name": "registreer_bestelling",
    "description": "Registreer de gekochte kledingstukken uit de bestelmail in de dressing.",
    "input_schema": {
        "type": "object",
        "properties": {
            "winkel": {"type": "string"},
            "besteldatum": {"type": "string", "description": "JJJJ-MM-DD"},
            "stukken": {"type": "array", "items": {"type": "object", "properties": _ORDER_PIECE,
                                                    "required": ["naam", "categorie", "kleuren", "foto"]}},
        },
        "required": ["winkel", "stukken"],
    },
}

INSPIRATION_SYSTEM = """Je helpt iemand zijn of haar smaak in kleding onder woorden te brengen. Je krijgt foto's van outfits die die persoon mooi vindt: screenshots van Instagram of Pinterest, of de foto van een pin. Elke foto heeft een nummer [foto N] en zijn grootte in pixels.

Roep precies één keer de tool bewaar_inspiratie aan. Regels:
- looks: één per foto met een outfit (foto is het nummer N). Staan er op één screenshot meerdere outfits, kies het outfit dat het meest in beeld staat. Staat er geen outfit op (enkel tekst, een product zonder persoon of styling, iets anders), zet geen_outfit op true.
- vak: bij een screenshot de rechthoek rond het outfit als [links, boven, rechts, onder] in pixels van die foto, zonder de knoppen, tekst en balken van de app eromheen. Is de foto zelf al het outfit, laat vak weg.
- beschrijving: 1 à 2 zinnen over het outfit zoals een stylist het zou samenvatten, met de stukken, kleuren, silhouet en sfeer (bv. "Wijde ecru linnen broek met een strak zwart topje en platte sandalen; ontspannen en minimalistisch"). Geen merken die je niet kan lezen, niets over het gezicht of lichaam van de persoon.
- kleuren: gewone Nederlandse kleurnamen, de hoofdkleur eerst. stijl: 2 tot 5 korte stijltermen. stukken: de kledingstukken in gewone woorden (bv. "wijde linnen broek", "zwart topje").
- profiel: het stijlprofiel opnieuw geschreven uit alle outfits samen (de nieuwe en de eerdere), zodat een stylist er outfits mee kan kiezen uit iemands eigen kleerkast. 3 tot 6 korte zinnen of puntjes: kleuren en tinten, silhouetten en pasvormen, materialen, typische combinaties, de sfeer, en wat er nooit in voorkomt. Herhalingen tellen zwaarder dan één uitschieter. Een bestaand profiel neem je mee en vul je aan; spreekt het de foto's tegen, dan gaan de foto's voor.
- Schrijf in het Nederlands (Vlaams), kort en concreet."""

INSPIRATION_TOOL = {
    "name": "bewaar_inspiratie",
    "description": "Bewaar wat elke foto van een outfit laat zien, en het stijlprofiel dat eruit volgt.",
    "input_schema": {
        "type": "object",
        "properties": {
            "looks": {"type": "array", "items": {"type": "object", "properties": {
                "foto": {"type": "integer", "description": "Het nummer N van de foto"},
                "geen_outfit": {"type": "boolean", "description": "true als er geen outfit op de foto staat"},
                "vak": {"type": "array", "items": {"type": "integer"}, "minItems": 4, "maxItems": 4,
                        "description": "[links, boven, rechts, onder] van het outfit, in pixels van de foto"},
                "beschrijving": {"type": "string"},
                "kleuren": {"type": "array", "items": {"type": "string"}},
                "stijl": {"type": "array", "items": {"type": "string"}},
                "stukken": {"type": "array", "items": {"type": "string"}},
            }, "required": ["foto"]}},
            "profiel": {"type": "string", "description": "Het stijlprofiel, 3 tot 6 korte zinnen of puntjes"},
        },
        "required": ["looks", "profiel"],
    },
}

STYLIST_SYSTEM = """Je bent de persoonlijke stylist van iemand en stelt outfits samen uitsluitend met kleren uit zijn of haar eigen dressing. De gebruiker beschrijft een gelegenheid, het weer, een stemming of een stuk dat erin moet. Stel 1 tot 3 outfits voor, de beste eerst, en roep precies één keer de tool geef_outfits aan. Gebruik enkel id's uit de lijst.

Een outfit is compleet en draagbaar:
- Een bovenstuk en een onderstuk, of een kleed of jumpsuit; altijd schoenen als die in de lijst staan (ook als je ze in waarom of tip vernoemt: dan horen ze bij stukken); een jas of vest als het weer of de gelegenheid dat vraagt; eventueel één of twee accessoires.
- Nooit twee stukken die hetzelfde doen (twee broeken, twee paar schoenen).

Kies zoals een stylist dat doet:
- Het seizoen: kies stukken voor het seizoen van het moment waarover de vraag gaat (het staat bij de datum). Een stuk met andere seizoenen enkel als het weer er echt om vraagt (een warme dag in de herfst, een koude in de lente), en dan zo dat het outfit toch bij het seizoen past: in de herfst en winter donkerdere en warmere tinten en stoffen, in de lente en zomer lichtere. Een stuk zonder seizoenen kan het hele jaar. Op reis telt het seizoen niet: daar kies je enkel naar het weer ter plaatse (een zomerjurk voor de Canarische Eilanden in december).
- Het weer eerst: kijk naar de gevoelstemperatuur, regen en wind van het moment waarover de vraag gaat (vandaag, morgen, 's avonds). Onder 10 °C een warme laag en jas, boven 22 °C lichte stoffen; bij regen geen suède en een jas die ertegen kan. Gebruik de warmte (1-5) van elk stuk en laag op als het 's ochtends fris en 's middags warm is.
- De gelegenheid bepaalt hoe formeel (1-5): een sollicitatie of huwelijk vraagt meer dan een terrasje. Liever een tikje te verzorgd dan te slordig.
- Kleuren die samengaan: neutrale basis (marine, grijs, beige, zwart, wit, ecru) met hoogstens één of twee accentkleuren; patronen hoogstens één opvallend stuk, tenzij ze bewust combineren. Materialen en stijl in dezelfde lijn.
- Laat stukken die al lang niet of nog nooit gedragen zijn eens aan bod komen als ze goed passen, en wissel af met wat de laatste dagen al gedragen werd. Favorieten mogen vaker.
- Wat de gebruiker eerder droeg en hoe het viel (score op 5, notitie), telt mee om te kiezen tussen outfits die al goed passen.
- Outfits die de gebruiker afwees, stel je niet opnieuw voor (ook niet bijna hetzelfde). Leer van de reden: 'te formeel' bij een vraag voor kantoor betekent dat kantoor minder formeel mag; 'kleuren botsen' of 'combineer ik nooit' gaat over die combinatie van stukken; 'draag ik niet graag' over een van de stukken.
- Stuurt de gebruiker een vorig voorstel bij ('andere schoenen', 'minder chic'), geef dan alle outfits uit dat voorstel terug, in dezelfde volgorde, en verander in elk enkel wat gevraagd wordt; vraagt de gebruiker een heel ander idee, dan mag alles anders. Zeg in advies kort wat je veranderde, en noem daarbij enkel stukken die echt in het voorstel of in je nieuwe outfits staan.
- Staat er een stijl uit outfits die de gebruiker mooi vindt, kies dan in die geest: dezelfde kleuren, silhouetten en combinaties waar de dressing dat toelaat. Het is smaak, geen filter: het weer en de gelegenheid gaan voor. Vraagt de gebruiker een look na te maken, benader die zo dicht mogelijk met eigen kleren en zeg in ontbreekt welk stuk de look echt zou afmaken.

Harde voorwaarden in de vraag (een kleur, een bepaald stuk, 'geen jeans', 'iets in het rood') zijn een filter, geen voorkeur. Ontbreekt er iets wezenlijks in de dressing voor deze vraag (bv. geen regenjas, geen nette schoenen), zeg dat kort in ontbreekt; dat is een tip om te kopen. Liever één goed outfit dan drie halve.

Schrijf in het Nederlands (Vlaams), warm maar kort."""

WEATHER_TOOL = {
    "name": "weer_opvragen",
    "description": "De weersvoorspelling voor een plaats en een of meer dagen (tot 16 dagen vooruit).",
    "input_schema": {
        "type": "object",
        "properties": {
            "plaats": {"type": "string", "description": "Stad of dorp, bv. 'Amsterdam' of 'Knokke'"},
            "van": {"type": "string", "description": "Eerste dag, JJJJ-MM-DD"},
            "tot": {"type": "string", "description": "Laatste dag, JJJJ-MM-DD; weglaten voor één dag"},
        },
        "required": ["plaats", "van"],
    },
}

OUTFIT_TOOL = {
    "name": "geef_outfits",
    "description": "Geef de voorgestelde outfits uit de dressing voor de vraag van de gebruiker.",
    "input_schema": {
        "type": "object",
        "properties": {
            "advies": {"type": "string", "description": "Kort advies (1-2 zinnen), bv. over het weer of de dresscode"},
            "outfits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "titel": {"type": "string", "description": "Korte naam van de look, bv. 'Smart casual in aardetinten'"},
                        "stukken": {"type": "array", "items": {"type": "string"}, "description": "id's van de stukken uit de lijst"},
                        "waarom": {"type": "string", "description": "Waarom dit past (1-2 zinnen)"},
                        "tip": {"type": "string", "description": "Een stylingtip in één zin (mouwen oprollen, hemd half insteken, ...)"},
                    },
                    "required": ["titel", "stukken", "waarom"],
                },
            },
            "ontbreekt": {"type": "string", "description": "Enkel als er iets wezenlijks ontbreekt in de dressing voor deze vraag (1 zin)"},
        },
        "required": ["advies", "outfits"],
    },
}
