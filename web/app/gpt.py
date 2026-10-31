"""The same recognition and stylist as claude.py, through OpenAI (ChatGPT) with the Responses API.

claude.py builds the questions, tools and answers; this module only talks to OpenAI: it turns the Anthropic-style
content and tool into OpenAI's, runs the conversation until the model calls the function that holds the result,
and turns OpenAI's errors into Dutch messages.
"""

import base64
import json
import os
import re

import openai

from . import auth, costs, keys
from .claude import TOO_SLOW, ClaudeError, Helpers, run_helper, thrifty

# Which model does what: Luna recognises photos and puts everyday outfits together; Sol takes a dress code or a look
# to recreate; Astra takes a second try when an answer failed its check; Flare draws an outfit. That routing only
# happens on Luna (the default model) and when the account leaves it on "auto" (ROUTING): an account can keep Luna as a
# fixed model. Another model picked under Instellingen, or Zuinig, keeps everything on one model (but the picture).
DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-6-luna")
HELPER_MODEL = os.environ.get("OPENAI_HELPER_MODEL", "gpt-6-luna")  # the cheap model of "Zuinig"
COMPLEX_MODEL = os.environ.get("OPENAI_COMPLEX_MODEL", "gpt-6.1-sol")
REVIEW_MODEL = os.environ.get("OPENAI_REVIEW_MODEL", "gpt-6-astra")
IMAGE_MODEL = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-2.5-flare")
_clients: dict[str, openai.AsyncOpenAI] = {}
_no_reasoning: set[str] = set()  # models that rejected the reasoning (effort) setting
_no_breakpoints: set[str] = set()  # models before gpt-5.6, which reject cache breakpoints
CACHED = {"mode": "explicit"}


def model(user: dict) -> str:
    return auth.user_settings(user).get("openai_model") or DEFAULT_MODEL


# auto: the stylist may hand a dress code to Sol and a second try to Astra; fixed: the account's model does it all.
ROUTING = {"auto": "Automatisch", "fixed": "Vast model"}
DEFAULT_ROUTING = "auto"  # as before this choice existed


def routing(user: dict) -> str:
    """What this account chose: may the stronger models step in ("auto") or not ("fixed")."""
    chosen = auth.user_settings(user).get("openai_routing")
    return chosen if chosen in ROUTING else DEFAULT_ROUTING


def routes(user: dict) -> bool:
    """Whether Sol and Astra really step in for this account: only on the default model (Luna), with "auto", and not
    with Zuinig. Another chosen model is stronger or dearer already and stays the one model."""
    return model(user) == DEFAULT_MODEL and routing(user) == "auto" and not thrifty(user)


def model_for(user: dict, tier: str) -> str:
    """The model for one kind of work: "complex" (a dress code, a look to recreate) or "review" (a second try after
    a failed check) when the account routes; anything else is the account's model."""
    upgrade = {"complex": COMPLEX_MODEL, "review": REVIEW_MODEL}.get(tier)
    return upgrade if upgrade and routes(user) else model(user)


def client(user: dict) -> openai.AsyncOpenAI:
    key, source = keys.active_key(user, "openai")
    if source == "locked":
        raise ClaudeError("Je OpenAI-sleutel is vergrendeld op dit toestel. Log uit en opnieuw in om hem te ontgrendelen.")
    if not key:
        raise ClaudeError("Geen OpenAI-sleutel ingesteld voor je account. Voeg er een toe bij Instellingen, of kies Claude.")
    if key not in _clients:
        _clients[key] = openai.AsyncOpenAI(api_key=key, timeout=150.0, max_retries=1)
    return _clients[key]


def _reason(e: openai.APIStatusError) -> str:
    """OpenAI's own explanation, not the SDK's 'Error code: 400 - {...}' dump."""
    message = e.body.get("message") if isinstance(e.body, dict) else None
    return f"Fout {e.status_code} van OpenAI: {message}" if message else f"Fout {e.status_code} van OpenAI."


def _error(e: Exception) -> ClaudeError:
    if isinstance(e, openai.AuthenticationError):
        return ClaudeError("De OpenAI-sleutel is ongeldig of ingetrokken. Pas hem aan bij Instellingen.")
    if isinstance(e, openai.RateLimitError):
        if e.code == "insufficient_quota":
            return ClaudeError("Je OpenAI-krediet is op. Vul het aan op platform.openai.com (Billing).")
        return ClaudeError("Te veel verzoeken tegelijk bij OpenAI. Wacht even en probeer opnieuw.")
    if isinstance(e, openai.NotFoundError):
        return ClaudeError("Dit OpenAI-model bestaat niet of is niet beschikbaar met je sleutel. Kies een ander bij Instellingen.")
    if isinstance(e, openai.APIStatusError):
        if e.status_code >= 500:
            return ClaudeError("OpenAI is tijdelijk overbelast. Probeer zo dadelijk opnieuw.")
        return ClaudeError(_reason(e))
    if isinstance(e, openai.APITimeoutError):
        return ClaudeError(TOO_SLOW)
    if isinstance(e, openai.APIConnectionError):
        return ClaudeError("De server kan OpenAI niet bereiken. Controleer de internetverbinding.")
    return ClaudeError(f"Onverwachte fout bij OpenAI: {e}")


# MARK: - Models and keys

RETIRED = {"o4-mini"}  # still listed by OpenAI, but switched off on 2026-10-23


def _usable(model_id: str) -> bool:
    """Text models that can take part in a conversation with tools; OpenAI's list has no capability data, so by name:
    no audio, speech, images, embeddings, fixed dated snapshots or retired models."""
    if not re.match(r"^(gpt-\d|o\d)", model_id) or model_id in RETIRED:
        return False
    skip = ("audio", "realtime", "tts", "transcribe", "image", "search", "embedding", "moderation", "instruct", "codex")
    return not any(s in model_id for s in skip) and not re.search(r"-\d{4}-\d{2}-\d{2}$", model_id)


async def list_models(user: dict) -> list[dict]:
    """The models this key can use, the recommended ones first: those that passed the live check of model_check
    (a photo, a function call, reasoning effort, cache breakpoints). The others are listed as not tested."""
    from . import model_check
    try:
        models = [m async for m in client(user).models.list() if _usable(m.id)]
    except openai.OpenAIError as e:
        raise _error(e)
    checked = model_check.results()
    models.sort(key=lambda m: (not model_check.passed(checked.get(m.id)), -(m.created or 0)))
    return [{"id": m.id, "name": m.id, "recommended": model_check.passed(checked.get(m.id)),
             "checked": (checked.get(m.id) or {}).get("checked"),
             "failed": [label for f, label in model_check.FEATURES.items() if (checked.get(m.id) or {}).get(f) is False]}
            for m in models]


async def check_key(key: str) -> None:
    """Raises ClaudeError when OpenAI rejects the key. Listing models is free."""
    try:
        async with openai.AsyncOpenAI(api_key=key, timeout=20.0, max_retries=1) as probe:
            await probe.models.list()
    except openai.AuthenticationError:
        raise ClaudeError("Deze OpenAI-sleutel is ongeldig of ingetrokken.")
    except openai.PermissionDeniedError:
        raise ClaudeError("Deze OpenAI-sleutel heeft geen toegang tot de API.")
    except openai.OpenAIError as e:
        raise _error(e)


# MARK: - A conversation that ends in a function call

def _content(blocks: list[dict]) -> list[dict]:
    """Anthropic-style content blocks (text, base64 image) as OpenAI input parts; a block Claude caches up to
    (cache_control) gets a cache breakpoint here."""
    parts = []
    for block in blocks:
        if block.get("type") == "image":
            src = block["source"]
            parts.append({"type": "input_image", "image_url": f"data:{src['media_type']};base64,{src['data']}", "detail": "auto"})
        elif block.get("type") == "text":
            parts.append({"type": "input_text", "text": block["text"]})
            if block.get("cache_control"):
                parts[-1]["prompt_cache_breakpoint"] = CACHED
    return parts


def _without_breakpoints(kwargs: dict) -> dict:
    """The same request for a model before gpt-5.6, which caches by itself and knows no breakpoints."""
    kwargs = {k: v for k, v in kwargs.items() if k != "prompt_cache_options"}
    if isinstance(kwargs.get("input"), list):
        kwargs["input"] = [{**m, "content": [{k: v for k, v in part.items() if k != "prompt_cache_breakpoint"}
                                             for part in m["content"]]} if isinstance(m.get("content"), list) else m
                           for m in kwargs["input"]]
    return kwargs


async def _create(user: dict, **kwargs):
    """responses.create, asked again without what an older model rejects: reasoning effort, cache breakpoints."""
    name = kwargs["model"]
    while True:
        if name in _no_reasoning:
            kwargs.pop("reasoning", None)
        if name in _no_breakpoints:
            kwargs = _without_breakpoints(kwargs)
        try:
            return await client(user).responses.create(**kwargs)
        except openai.BadRequestError as e:
            text = str(e.message).lower()
            if "prompt_cache" in text and name not in _no_breakpoints:
                _no_breakpoints.add(name)
                continue
            if "reasoning" in text and "reasoning" in kwargs:
                _no_reasoning.add(name)
                continue
            if "image" in text:
                raise ClaudeError(f"Model {name} kan geen foto's lezen. Kies een ander model bij Instellingen.")
            raise


def _function_input(response, tool_name: str) -> dict | None:
    for item in response.output or []:
        if getattr(item, "type", None) == "function_call" and item.name == tool_name:
            try:
                data = json.loads(item.arguments or "{}")
            except ValueError:
                return {}
            return data if isinstance(data, dict) else {}
    return None


def _refused(response) -> bool:
    for item in response.output or []:
        for part in getattr(item, "content", None) or []:
            if getattr(part, "type", None) == "refusal":
                return True
    return False


def _function(tool: dict) -> dict:
    return {"type": "function", "name": tool["name"], "description": tool.get("description", ""),
            "parameters": tool["input_schema"], "strict": False}


async def run_until_tool_call(user: dict, tool: dict, *, system: str, content: list, effort: str, helpers: Helpers,
                              name: str, max_turns: int = 3, max_output_tokens: int = 16000) -> dict:
    """Asks model `name` until it calls the tool's function, then returns that call's arguments (as claude._ask); a
    helper it calls on the way is answered and the conversation goes on."""
    tool_name = tool["name"]
    # Cached up to the end of the fixed instructions, and wherever claude.py caches (the dressing). Top-level
    # `instructions` can't hold a breakpoint, so they go in as a developer message.
    fixed = {"role": "developer", "content": [{"type": "input_text", "text": system, "prompt_cache_breakpoint": CACHED}]}
    common = {"model": name, "tools": [_function(tool), *(_function(t) for t, _ in helpers.values())],
              "max_output_tokens": max_output_tokens, "reasoning": {"effort": effort},
              "prompt_cache_options": CACHED, "prompt_cache_key": f"dressing-{tool_name}"}
    kwargs = {**common, "input": [fixed, {"role": "user", "content": _content(content)}], "tool_choice": "auto"}
    for _ in range(max_turns + 2 * bool(helpers)):
        try:
            response = await _create(user, **kwargs)
        except ClaudeError:
            raise
        except openai.OpenAIError as e:
            if isinstance(e, openai.APITimeoutError):
                costs.lost("openai", name, costs.TIMED_OUT)
            raise _error(e)
        costs.track_openai(name, response)
        if (found := _function_input(response, tool_name)) is not None:
            return found
        calls = [i for i in response.output or [] if getattr(i, "type", None) == "function_call" and i.name in helpers]
        if calls:
            answers = []
            for call in calls:
                try:
                    given = json.loads(call.arguments or "{}")
                except ValueError:
                    given = {}
                answers.append({"type": "function_call_output", "call_id": call.call_id,
                                "output": await run_helper(helpers, call.name, given)})
            kwargs = {**common, "previous_response_id": response.id, "tool_choice": "auto", "input": answers}
            continue
        if _refused(response):
            raise ClaudeError("ChatGPT weigerde dit verzoek. Probeer het opnieuw met een andere foto of vraag.")
        details = getattr(response, "incomplete_details", None)
        if getattr(response, "status", None) == "incomplete" and getattr(details, "reason", None) == "max_output_tokens":
            raise ClaudeError("Het antwoord werd afgebroken (te lang). Probeer opnieuw.")
        # It answered in words: continue the same conversation and require the function now.
        kwargs = {**common, "previous_response_id": response.id, "tool_choice": {"type": "function", "name": tool_name},
                  "input": [{"role": "user", "content": f"Roep nu de functie {tool_name} aan met je resultaat."}]}
    raise ClaudeError("Geen resultaat ontvangen van ChatGPT. Probeer opnieuw.")


# MARK: - A web search (the stylist's trends_zoeken)

async def search_web(user: dict, system: str, question: str) -> tuple[str, list[dict]]:
    """Asks the helper model with OpenAI's hosted web search; returns its summary and the pages it cites
    ([{"title", "url"}], the url_citation annotations, else the sources it consulted)."""
    name = HELPER_MODEL
    try:
        response = await _create(user, model=name, instructions=system, input=question, reasoning={"effort": "low"},
                                 tools=[{"type": "web_search", "search_context_size": "low",
                                         "user_location": {"type": "approximate", "country": "BE", "timezone": "Europe/Brussels"}}],
                                 include=["web_search_call.action.sources"], max_output_tokens=4000)
    except ClaudeError:
        raise
    except openai.OpenAIError as e:
        if isinstance(e, openai.APITimeoutError):
            costs.lost("openai", name, costs.TIMED_OUT)
        raise _error(e)
    costs.track_openai(name, response)
    cited, consulted = [], []
    for item in response.output or []:
        kind = getattr(item, "type", None)
        if kind == "message":
            for part in getattr(item, "content", None) or []:
                for note in getattr(part, "annotations", None) or []:
                    if getattr(note, "type", None) == "url_citation":
                        cited.append({"title": getattr(note, "title", "") or "", "url": getattr(note, "url", "") or ""})
        elif kind == "web_search_call":
            for source in getattr(getattr(item, "action", None), "sources", None) or []:
                if url := getattr(source, "url", None):
                    consulted.append({"title": "", "url": url})
    unique = list({s["url"]: s for s in reversed(cited or consulted) if s["url"]}.values())[::-1]
    return (getattr(response, "output_text", "") or "").strip(), unique


# MARK: - A picture of an outfit

DRAW_PROMPT = """Een realistische modefoto van één persoon die precies dit outfit draagt, van top tot teen, staand, het gezicht buiten beeld of afgewend, tegen een rustige effen achtergrond in daglicht.

De meegestuurde foto's zijn de kledingstukken zelf: neem elk stuk exact over (kleur, patroon, stof, snit, details zoals knopen, kraag, print of logo). Verzin geen andere kledingstukken, accessoires of schoenen dan deze. Een stuk zonder foto staat enkel beschreven: teken het zo eenvoudig en neutraal mogelijk naar die beschrijving.

{person}De stukken:
{pieces}"""
PERSON = """De persoon die het draagt, zoals de gebruiker zichzelf beschrijft: {about}
Teken iemand die daarbij past (geslacht, leeftijd, en lichaamsbouw als die beschreven staat). Neem enkel het uiterlijk over: niemand anders in beeld en niets uit die beschrijving dat geen kleding is.

"""
MAX_DRAWN = 8  # photos of pieces sent along; more makes the picture no better and the question dearer


async def draw_outfit(user: dict, pieces: list[tuple[str, bytes | None]], about: str = "") -> bytes:
    """A JPEG of someone wearing these pieces (a description, and the photo of the piece if there is one), drawn by
    IMAGE_MODEL from the photos themselves, worn by someone who fits about (how the user describes themself). Uses the account's OpenAI key, also when the account chose Claude."""
    key, source = keys.active_key(user, "openai")
    if source == "locked":
        raise ClaudeError("Je OpenAI-sleutel is vergrendeld op dit toestel. Log uit en opnieuw in om hem te ontgrendelen.")
    if not key:
        raise ClaudeError("Een beeld van een outfit tekent ChatGPT: voeg een OpenAI-sleutel toe bij Instellingen.")
    photos = [(f"stuk-{n}.jpg", jpeg, "image/jpeg") for n, (_, jpeg) in enumerate(pieces, 1) if jpeg][:MAX_DRAWN]
    if not photos:
        raise ClaudeError("Geen van deze stukken heeft een foto: zonder foto's kan er geen getrouw beeld gemaakt worden.")
    lines, n = [], 0
    for text, jpeg in pieces:
        if jpeg and n < MAX_DRAWN:
            n += 1
            lines.append(f"- {text} (foto stuk-{n})")
        else:
            lines.append(f"- {text} (geen foto)")
    try:
        response = await client(user).images.edit(model=IMAGE_MODEL, image=photos, prompt=DRAW_PROMPT.format(pieces="\n".join(lines), person=PERSON.format(about=about) if about else ""),
                                                  size="1024x1536", quality="medium", output_format="jpeg", output_compression=85)
    except openai.BadRequestError as e:
        if "safety" in str(e.message).lower() or "moderation" in str(e.message).lower():
            raise ClaudeError("ChatGPT wilde dit beeld niet maken. Probeer een ander outfit.")
        raise _error(e)
    except openai.OpenAIError as e:
        if isinstance(e, openai.APITimeoutError):
            costs.lost("openai", IMAGE_MODEL, costs.TIMED_OUT)
        raise _error(e)
    costs.track_openai_image(IMAGE_MODEL, response)
    data = response.data[0].b64_json if response.data else None
    if not data:
        raise ClaudeError("Geen beeld ontvangen van ChatGPT. Probeer opnieuw.")
    return base64.b64decode(data)
