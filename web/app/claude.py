"""Recognising a piece of clothing from a photo, and the stylist that puts outfits together from the dressing:
the questions, tools and answers, and the Claude API calls."""

import asyncio
import base64
import contextvars
import os
from datetime import date

import anthropic

from . import auth, db, keys

DEFAULT_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
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
    key, source = keys.active_key(user)
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


async def _ask(user: dict, tool: dict, *, system: str, content: list, effort: str = "low") -> dict:
    """Sends the question until Claude calls `tool`, then returns that call's input. Forced tool use isn't accepted by
    the current models, so the tool is offered and asked for by name."""
    name = model(user)
    messages: list[dict] = [{"role": "user", "content": content}]
    for _ in range(3):
        try:
            response = await _create(
                user,
                model=name,
                max_tokens=16000,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=messages,
                tools=[tool],
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

        for block in response.content:
            if block.type == "tool_use" and block.name == tool["name"]:
                return dict(block.input) if isinstance(block.input, dict) else {}
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
    got = await _timed(_ask(user, ITEM_TOOL, system=IDENTIFY_SYSTEM, content=content))
    info = db.clean_item({
        "name": got.get("naam"), "category": got.get("categorie"), "kind": got.get("soort"), "colors": got.get("kleuren"),
        "pattern": got.get("patroon"), "material": got.get("materiaal"), "brand": got.get("merk"), "fit": got.get("pasvorm"),
        "seasons": got.get("seizoenen"), "warmth": got.get("warmte"), "formality": got.get("formaliteit"),
        "occasions": got.get("gelegenheden"), "style_tags": got.get("stijl"), "description": got.get("beschrijving"),
        "care": got.get("onderhoud"),
    })
    return {k: v for k, v in info.items() if v not in (None, "", [])}


# MARK: - The stylist

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
                          profile: str = "") -> dict:
    """1 to 3 outfits from the pieces in the dressing for this question (an occasion, the weather, a mood)."""
    wear: dict[str, dict] = {}
    for log in sorted(worn, key=lambda l: l["date"]):
        for item_id in log.get("item_ids", []):
            w = wear.setdefault(item_id, {"count": 0, "last": ""})
            w["count"] += 1
            w["last"] = log["date"][:10]
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
    wardrobe = "Mijn dressing (één stuk per regel, id vooraan):\n" + "\n".join(_item_line(i, wear) for i in available)
    if profile:
        wardrobe = f"Over mij: {profile}\n\n{wardrobe}"
    if taste:
        wardrobe += "\n\nWat ik eerder droeg en hoe het viel:\n" + "\n".join(taste)
    question = f"Vandaag is het {date.today().isoformat()}."
    if weather:
        question += f"\n{weather}"
    question += f"\nVraag: {query}"
    # The dressing first, cached on its own: a next question within a few minutes only pays for the question.
    content = [{"type": "text", "text": wardrobe, "cache_control": {"type": "ephemeral"}}, {"type": "text", "text": question}]
    report(step="Outfits samenstellen")
    got = await _timed(_ask(user, OUTFIT_TOOL, system=STYLIST_SYSTEM, content=content))
    known = {i["id"] for i in available}
    outfits = []
    for o in got.get("outfits") or []:
        if not isinstance(o, dict):
            continue
        ids = list(dict.fromkeys(str(i) for i in o.get("stukken") or [] if str(i) in known))
        if ids:
            outfits.append({"title": o.get("titel") or "", "item_ids": ids, "reason": o.get("waarom") or "",
                            "tip": o.get("tip") or ""})
    return {"advice": got.get("advies") or "", "outfits": outfits, "missing": got.get("ontbreekt") or ""}


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

STYLIST_SYSTEM = """Je bent de persoonlijke stylist van iemand en stelt outfits samen uitsluitend met kleren uit zijn of haar eigen dressing. De gebruiker beschrijft een gelegenheid, het weer, een stemming of een stuk dat erin moet. Stel 1 tot 3 outfits voor, de beste eerst, en roep precies één keer de tool geef_outfits aan. Gebruik enkel id's uit de lijst.

Een outfit is compleet en draagbaar:
- Een bovenstuk en een onderstuk, of een kleed of jumpsuit; schoenen als die in de dressing hangen; een jas of vest als het weer of de gelegenheid dat vraagt; eventueel één of twee accessoires.
- Nooit twee stukken die hetzelfde doen (twee broeken, twee paar schoenen).

Kies zoals een stylist dat doet:
- Het weer eerst: kijk naar de gevoelstemperatuur, regen en wind van het moment waarover de vraag gaat (vandaag, morgen, 's avonds). Onder 10 °C een warme laag en jas, boven 22 °C lichte stoffen; bij regen geen suède en een jas die ertegen kan. Gebruik de warmte (1-5) van elk stuk en laag op als het 's ochtends fris en 's middags warm is.
- De gelegenheid bepaalt hoe formeel (1-5): een sollicitatie of huwelijk vraagt meer dan een terrasje. Liever een tikje te verzorgd dan te slordig.
- Kleuren die samengaan: neutrale basis (marine, grijs, beige, zwart, wit, ecru) met hoogstens één of twee accentkleuren; patronen hoogstens één opvallend stuk, tenzij ze bewust combineren. Materialen en stijl in dezelfde lijn.
- Laat stukken die al lang niet of nog nooit gedragen zijn eens aan bod komen als ze goed passen, en wissel af met wat de laatste dagen al gedragen werd. Favorieten mogen vaker.
- Wat de gebruiker eerder droeg en hoe het viel (score op 5, notitie), telt mee om te kiezen tussen outfits die al goed passen.

Harde voorwaarden in de vraag (een kleur, een bepaald stuk, 'geen jeans', 'iets in het rood') zijn een filter, geen voorkeur. Ontbreekt er iets wezenlijks in de dressing voor deze vraag (bv. geen regenjas, geen nette schoenen), zeg dat kort in ontbreekt; dat is een tip om te kopen. Liever één goed outfit dan drie halve.

Schrijf in het Nederlands (Vlaams), warm maar kort."""

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
