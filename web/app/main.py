"""Dressing server: JSON API + the installable web app."""

import asyncio
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import time
from contextlib import asynccontextmanager, closing
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps
from pydantic import BaseModel

from . import audit, auth, claude, db, gpt, keys, weather

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
BACKUPS_TO_KEEP = 14


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    task = asyncio.create_task(_daily_maintenance())
    yield
    task.cancel()


app = FastAPI(title="Dressing", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

# Every API route takes one of these: the signed-in account, or the administrator.
User = Annotated[dict, Depends(auth.require_user)]
Admin = Annotated[dict, Depends(auth.require_admin)]


def require_ai_key(user: User) -> dict:
    """For a request that asks the AI: the service the account chose must have a key this sign-in can open. Checked
    first, so nothing is uploaded for an analysis that can't happen."""
    provider = keys.provider(user)
    key, source = keys.active_key(user, provider)
    name = keys.PROVIDERS[provider]["name"]
    if source == "locked":
        raise HTTPException(409, f"Je {name}-sleutel is vergrendeld op dit toestel. Log uit en opnieuw in om hem te ontgrendelen.")
    if not key:
        raise HTTPException(400, f"Stel eerst een API-sleutel voor {name} in bij Instellingen: zonder sleutel kan de app geen "
                                 "kleren herkennen of outfits voorstellen.")
    return user


AIUser = Annotated[dict, Depends(require_ai_key)]

# Only the app's own scripts run, and it only talks to this server.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
       "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")

# The most a request may send; only a signed-in account may upload photos.
MAX_BODY, MAX_UPLOAD = 1_000_000, 30_000_000
UPLOADS = {"/api/identify"}


class LimitBody:
    """Refuses a request body above its limit: at once when it says its size, and else as soon as more arrives."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS"):
            return await self.app(scope, receive, send)
        limit = MAX_BODY
        if scope["path"] in UPLOADS or re.fullmatch(r"/api/items/[^/]+/photo", scope["path"]):
            if auth.current_user(Request(scope)):
                limit = MAX_UPLOAD
        length = dict(scope["headers"]).get(b"content-length", b"")
        too_large = JSONResponse(status_code=413, content={"detail": "Dit is te groot om te versturen."})
        if length.isdigit() and int(length) > limit:
            return await too_large(scope, receive, send)
        received = 0

        async def limited():
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > limit:
                raise HTTPException(413, "Dit is te groot om te versturen.")
            return message

        await self.app(scope, limited, send)


app.add_middleware(LimitBody)


@app.middleware("http")
async def always_fresh_app(request: Request, call_next):
    """The app's own files may be kept but must be checked with the server before use, so an update shows at once."""
    response = await call_next(request)
    if not request.url.path.startswith(("/api/", "/photos/")) and "cache-control" not in response.headers:
        response.headers["Cache-Control"] = "no-cache"
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Content-Security-Policy", CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if request.headers.get("x-forwarded-proto") == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return response


@app.middleware("http")
async def only_from_the_app(request: Request, call_next):
    """Changes must come from the app itself, which adds this header to every request; another website can't."""
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith("/api/") \
            and request.headers.get("x-dressing") != "1":
        return JSONResponse(status_code=403, content={"detail": "Verzoek geweigerd. Vernieuw de app en probeer opnieuw."})
    return await call_next(request)


@app.exception_handler(claude.ClaudeError)
async def claude_error(_, exc: claude.ClaudeError):
    return JSONResponse(status_code=502, content={"detail": str(exc)})


@app.exception_handler(weather.WeatherError)
async def weather_error(_, exc: weather.WeatherError):
    return JSONResponse(status_code=502, content={"detail": str(exc)})


@app.get("/api/health")
def health():
    return {"ok": True}


# MARK: - Helpers

def _jpeg(data: bytes, max_side: int, quality: int = 80) -> bytes:
    """Decodes any uploaded image, applies EXIF rotation, scales it down and re-encodes as JPEG."""
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img).convert("RGB")
    except Exception:
        raise HTTPException(400, "Dit bestand kon niet als foto gelezen worden.")
    img.thumbnail((max_side, max_side))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=quality)
    return out.getvalue()


def _take_tmp_photo(token: str, item_id: str) -> bool:
    """Moves the photo kept from recognising a piece to that piece."""
    tmp = db.TMP_DIR / f"{token}.jpg"
    if not token or not token.isalnum() or not tmp.exists():
        return False
    shutil.move(tmp, db.PHOTO_DIR / f"{item_id}.jpg")
    return True


def _remove_photo(item_id: str) -> None:
    (db.PHOTO_DIR / f"{item_id}.jpg").unlink(missing_ok=True)
    (db.PHOTO_DIR / "thumbs" / f"{item_id}.jpg").unlink(missing_ok=True)


SAFE_ID = re.compile(r"[A-Za-z0-9-]{1,64}")  # ids also name photo files


def _get_item(conn: sqlite3.Connection, item_id: str, user: dict) -> sqlite3.Row:
    """A piece in this account's dressing; anyone else's is simply "not found"."""
    row = conn.execute("SELECT * FROM items WHERE id = ? AND user_id = ?", (item_id, user["id"])).fetchone()
    if not row:
        raise HTTPException(404, "Kledingstuk niet gevonden.")
    return row


def _own_item_ids(conn, user: dict) -> set[str]:
    return {r["id"] for r in conn.execute("SELECT id FROM items WHERE user_id = ?", (user["id"],))}


# MARK: - Accounts

class Credentials(BaseModel):
    username: str
    password: str


@app.get("/api/auth")
def auth_status(request: Request):
    """Who is signed in, and whether the very first (administrator) account still has to be made."""
    with db.connect() as conn:
        setup_needed = not conn.execute("SELECT 1 FROM users LIMIT 1").fetchone()
    return {"setup_needed": setup_needed, "user": auth.current_user(request)}


@app.post("/api/auth/setup")
def setup(body: Credentials, request: Request, response: Response):
    """Creates the administrator; only possible while there are no accounts."""
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")  # two setups at the same moment can't both make an administrator
        if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            raise HTTPException(403, "Er bestaat al een beheerder. Log in.")
        user = auth.create_user(conn, body.username, body.password, is_admin=True)
        auth.start_session(conn, request, response, user["id"], auth.unlock(conn, user["id"], body.password))
        audit.record(request, "setup", user["username"], user["id"], conn)
    return {"user": user}


@app.post("/api/auth/login")
def login(body: Credentials, request: Request, response: Response):
    audit.check_login(request, body.username)  # a pause after too many wrong passwords
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (body.username.strip(),)).fetchone()
        # Always run a password check, so a wrong username takes as long as a wrong password.
        ok = auth.verify_password(body.password, row["password_hash"] if row else auth.DUMMY_HASH)
    if not row or not ok:
        audit.login_failed(request, body.username)
        raise HTTPException(401, "Gebruikersnaam of wachtwoord klopt niet.")
    audit.clear_failures(request, body.username)
    with db.connect() as conn:
        auth.start_session(conn, request, response, row["id"], auth.unlock(conn, row["id"], body.password))
        audit.record(request, "login", row["username"], row["id"], conn)
    return {"user": auth.public_user(row)}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    if user := auth.current_user(request):
        audit.record(request, "logout", user["username"], user["id"])
    auth.end_session(request, response)
    return {"ok": True}


class PasswordChangeIn(BaseModel):
    current: str
    new: str


@app.put("/api/auth/password")
def change_password(body: PasswordChangeIn, user: User, request: Request, response: Response):
    audit.check_login(request, user["username"])
    with db.connect() as conn:
        row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user["id"],)).fetchone()
    if not auth.verify_password(body.current, row["password_hash"]):
        audit.login_failed(request, user["username"])
        raise HTTPException(400, "Je huidige wachtwoord klopt niet.")
    with db.connect() as conn:
        data_key = auth.set_password(conn, user["id"], body.new, auth.unlock(conn, user["id"], body.current))
        auth.start_session(conn, request, response, user["id"], data_key)  # stay signed in here; elsewhere signed out
        audit.record(request, "password_changed", user["username"], user["id"], conn)
    return {"ok": True}


@app.get("/api/users")
def list_users(admin: Admin):
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY created_at").fetchall()
        counts = dict(conn.execute("SELECT user_id, COUNT(*) FROM items GROUP BY user_id").fetchall())
    return [{**auth.public_user(r), "items": counts.get(r["id"], 0)} for r in rows]


@app.post("/api/users")
def add_user(body: Credentials, admin: Admin, request: Request):
    with db.connect() as conn:
        user = auth.create_user(conn, body.username, body.password)
        audit.record(request, "account_created", user["username"], user["id"], conn)
    return user


class PasswordResetIn(BaseModel):
    password: str


@app.put("/api/users/{user_id}/password")
def reset_password(user_id: str, body: PasswordResetIn, admin: Admin, request: Request):
    with db.connect() as conn:
        row = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Account niet gevonden.")
        auth.set_password(conn, user_id, body.password)  # the vault can't be opened any more: the key goes too
        audit.record(request, "password_reset", row["username"], user_id, conn)
    return {"ok": True}


@app.delete("/api/users/{user_id}")
def delete_user(user_id: str, admin: Admin, request: Request):
    if user_id == admin["id"]:
        raise HTTPException(400, "Je kan je eigen account hier niet verwijderen.")
    with db.connect() as conn:
        row = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Account niet gevonden.")
        items = [r["id"] for r in conn.execute("SELECT id FROM items WHERE user_id = ?", (user_id,))]
        for table in ("worn", "items", "sessions"):
            conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        audit.record(request, "account_deleted", row["username"], None, conn)
    for item_id in items:
        _remove_photo(item_id)
    return {"ok": True}


@app.get("/api/audit")
def audit_log(user: User):
    return audit.entries(user)


# MARK: - State

def _version() -> str:
    """Set at build time by update.sh; when running from a git checkout (development), read from git."""
    if version := os.environ.get("APP_VERSION", "").strip():
        return version
    try:
        git = lambda *args: subprocess.run(["git", *args], cwd=Path(__file__).parent, capture_output=True, text=True,
                                           timeout=5, check=True).stdout.strip()
        return f"{git('rev-list', '--count', 'HEAD')} ({git('log', '-1', '--date=short', '--format=%cd, %h')})"
    except (OSError, subprocess.SubprocessError):
        return ""


VERSION = _version()


def _service(name: str):
    """The module that talks to an AI service: claude (Anthropic) or gpt (OpenAI)."""
    return {"claude": claude, "openai": gpt}[name]


def _provider_name(provider: str | None, user: dict) -> str:
    if not provider:
        return keys.provider(user)
    if provider not in keys.PROVIDERS:
        raise HTTPException(400, "Onbekende AI-dienst.")
    return provider


def _key_info(user: dict, provider: str = "claude") -> dict:
    key, source = keys.active_key(user, provider)
    return {"source": source, "hint": keys.hint(key) if source == "app" else ""}


def _ai_info(user: dict) -> dict:
    """Per service: its key (only a hint), its model and its default; plus which service is in use."""
    services = {name: {"name": spec["name"], "company": spec["company"], "console": spec["console"],
                       "api_key": _key_info(user, name), "model": _service(name).model(user),
                       "default_model": _service(name).DEFAULT_MODEL}
                for name, spec in keys.PROVIDERS.items()}
    return {"provider": keys.provider(user), "services": services}


@app.get("/api/state")
def state(user: User):
    settings = auth.user_settings(user)
    with db.connect() as conn:
        return {
            "user": user,
            "items": [db.item_row(r) for r in conn.execute("SELECT * FROM items WHERE user_id = ? ORDER BY date_added DESC", (user["id"],))],
            "worn": [db.worn_row(r) for r in conn.execute("SELECT * FROM worn WHERE user_id = ? ORDER BY date DESC", (user["id"],))],
            "categories": [{"key": k, "label": v} for k, v in db.CATEGORIES.items()],
            "statuses": [{"key": k, "label": v} for k, v in db.STATUSES.items()],
            "seasons": db.SEASONS, "formality": db.FORMALITY, "warmth": db.WARMTH,
            "ai": _ai_info(user),
            "place": settings.get("place"),
            "profile": settings.get("profile") or "",
            "version": VERSION,
        }


# MARK: - Settings: API key (write-only from the app: only a short hint ever comes back), model, profile, place

class ProviderIn(BaseModel):
    provider: str


@app.put("/api/settings/provider")
def set_provider(body: ProviderIn, user: User):
    """Claude or ChatGPT for this account's recognition and stylist."""
    auth.put_user_setting(user, "ai_provider", _provider_name(body.provider, user))
    return _ai_info(user)


class KeyIn(BaseModel):
    key: str
    provider: str = "claude"


@app.put("/api/settings/api-key")
async def set_api_key(body: KeyIn, user: User):
    provider = _provider_name(body.provider, user)
    key = "".join(body.key.split())  # drop spaces/newlines picked up while pasting
    if provider == "claude" and (not key.startswith("sk-ant-") or len(key) < 30):
        raise HTTPException(400, "Dit lijkt geen Anthropic API-sleutel (die begint met sk-ant-).")
    if provider == "openai" and (not key.startswith("sk-") or key.startswith("sk-ant-") or len(key) < 30):
        raise HTTPException(400, "Dit lijkt geen OpenAI API-sleutel (die begint met sk-, en niet met sk-ant-).")
    data_key = getattr(user, "data_key", None)
    if not data_key:
        raise HTTPException(409, "Log uit en opnieuw in om een sleutel te kunnen bewaren.")
    await _service(provider).check_key(key)  # only store a key the service accepts
    auth.put_user_setting(user, keys.PROVIDERS[provider]["secret"], auth.encrypt_secret(data_key, key))
    return {"api_key": {"source": "app", "hint": keys.hint(key)}}


@app.delete("/api/settings/api-key")
def clear_api_key(user: User, provider: str = "claude"):
    provider = _provider_name(provider, user)
    auth.put_user_setting(user, keys.PROVIDERS[provider]["secret"], None)
    return {"api_key": _key_info(user, provider)}


@app.get("/api/settings/models")
async def list_models(user: User, provider: str = ""):
    return await _service(_provider_name(provider, user)).list_models(user)


class ModelIn(BaseModel):
    model: str | None = None
    provider: str = ""


@app.put("/api/settings/model")
def set_model(body: ModelIn, user: User):
    """This account's model for a service; empty goes back to the default (from .env or the built-in one)."""
    provider = _provider_name(body.provider, user)
    chosen = (body.model or "").strip()
    if chosen and not re.fullmatch(r"[A-Za-z0-9._:-]{2,80}", chosen):
        raise HTTPException(400, "Onbekend model.")
    auth.put_user_setting(user, keys.PROVIDERS[provider]["model_setting"], chosen or None)
    return {"model": _service(provider).model(user)}


class ProfileIn(BaseModel):
    profile: str = ""


@app.put("/api/settings/profile")
def set_profile(body: ProfileIn, user: User):
    auth.put_user_setting(user, "profile", body.profile.strip()[:1500] or None)
    return {"ok": True}


class PlaceIn(BaseModel):
    place: str


@app.put("/api/settings/place")
async def set_place(body: PlaceIn, user: User):
    if not body.place.strip():
        auth.put_user_setting(user, "place", None)
        return {"place": None}
    place = await weather.find_place(body.place)
    auth.put_user_setting(user, "place", place)
    return {"place": place}


@app.get("/api/weather")
async def get_weather(user: User):
    place = auth.user_settings(user).get("place")
    if not place:
        raise HTTPException(400, "Stel eerst je woonplaats in bij Instellingen.")
    return await weather.forecast(place)


# MARK: - Items (the pieces in the dressing)

class ItemIn(BaseModel):
    data: dict
    photo_token: str = ""


@app.post("/api/items")
def create_item(body: ItemIn, user: User):
    data = db.clean_item(body.data)
    if not (data.get("name") or "").strip():
        raise HTTPException(400, "Geef het kledingstuk een naam.")
    data.setdefault("status", "dressing")
    item_id = db.new_id()
    has_photo = _take_tmp_photo(body.photo_token, item_id)
    with db.connect() as conn:
        conn.execute("INSERT INTO items (id, user_id, date_added, has_photo, data) VALUES (?, ?, ?, ?, ?)",
                     (item_id, user["id"], db.now_iso(), int(has_photo), json.dumps(data)))
        return db.item_row(_get_item(conn, item_id, user))


@app.patch("/api/items/{item_id}")
def update_item(item_id: str, body: dict, user: User):
    with db.connect() as conn:
        row = _get_item(conn, item_id, user)
        data = {**json.loads(row["data"]), **db.clean_item(body)}
        if not (data.get("name") or "").strip():
            raise HTTPException(400, "Geef het kledingstuk een naam.")
        conn.execute("UPDATE items SET data = ? WHERE id = ?", (json.dumps(data), item_id))
        return db.item_row(_get_item(conn, item_id, user))


@app.delete("/api/items/{item_id}")
def delete_item(item_id: str, user: User):
    """A piece leaves the dressing; outfits it was part of keep their other pieces."""
    with db.connect() as conn:
        _get_item(conn, item_id, user)
        conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
    _remove_photo(item_id)
    return {"ok": True}


@app.post("/api/items/{item_id}/photo")
async def set_item_photo(item_id: str, user: User, photo: UploadFile = File(...)):
    raw = await photo.read()
    jpeg = _jpeg(raw, 1200, 78)
    with db.connect() as conn:
        _get_item(conn, item_id, user)
        _remove_photo(item_id)
        (db.PHOTO_DIR / f"{item_id}.jpg").write_bytes(jpeg)
        conn.execute("UPDATE items SET has_photo = 1 WHERE id = ?", (item_id,))
        return db.item_row(_get_item(conn, item_id, user))


# MARK: - Worn (outfits that were worn)

class WornIn(BaseModel):
    item_ids: list[str]
    date: str = ""
    occasion: str = ""
    note: str = ""
    rating: int | None = None


def _worn_data(conn, body: WornIn, user: dict) -> tuple[str, dict]:
    ids = list(dict.fromkeys(i for i in body.item_ids if i in _own_item_ids(conn, user)))
    if not ids:
        raise HTTPException(400, "Kies minstens één kledingstuk.")
    day = body.date.strip() or date.today().isoformat()
    try:
        date.fromisoformat(day[:10])
    except ValueError:
        raise HTTPException(400, "Geef een geldige datum.")
    rating = body.rating if body.rating and 1 <= body.rating <= 5 else None
    return day[:10], {"item_ids": ids, "occasion": body.occasion.strip()[:200], "note": body.note.strip()[:2000], "rating": rating}


@app.post("/api/worn")
def add_worn(body: WornIn, user: User):
    with db.connect() as conn:
        day, data = _worn_data(conn, body, user)
        log_id = db.new_id()
        conn.execute("INSERT INTO worn (id, user_id, date, data) VALUES (?, ?, ?, ?)", (log_id, user["id"], day, json.dumps(data)))
        return db.worn_row(conn.execute("SELECT * FROM worn WHERE id = ?", (log_id,)).fetchone())


@app.put("/api/worn/{log_id}")
def update_worn(log_id: str, body: WornIn, user: User):
    with db.connect() as conn:
        if not conn.execute("SELECT 1 FROM worn WHERE id = ? AND user_id = ?", (log_id, user["id"])).fetchone():
            raise HTTPException(404, "Outfit niet gevonden.")
        day, data = _worn_data(conn, body, user)
        conn.execute("UPDATE worn SET date = ?, data = ? WHERE id = ?", (day, json.dumps(data), log_id))
        return db.worn_row(conn.execute("SELECT * FROM worn WHERE id = ?", (log_id,)).fetchone())


@app.delete("/api/worn/{log_id}")
def delete_worn(log_id: str, user: User):
    with db.connect() as conn:
        if not conn.execute("DELETE FROM worn WHERE id = ? AND user_id = ?", (log_id, user["id"])).rowcount:
            raise HTTPException(404, "Outfit niet gevonden.")
    return {"ok": True}


# MARK: - Claude
#
# Analyses run in the background when the app asks (?background=1): it gets a job number at once and asks every few
# seconds whether the answer is there. A phone never holds one request open for minutes, which Safari (and a
# connection that drops while the phone sleeps) breaks off.

_jobs: dict[str, dict] = {}
JOB_SECONDS = 30 * 60


async def _background(request: Request, user: dict, work):
    if request.query_params.get("background") != "1":
        return await work
    now = time.monotonic()
    for old in [j for j, x in _jobs.items() if now - x["at"] > JOB_SECONDS]:
        _jobs.pop(old)["task"].cancel()
    job, progress = secrets.token_urlsafe(18), {}
    token = claude.PROGRESS.set(progress)  # the task takes this along: what the analysis reports lands here
    try:
        _jobs[job] = {"user": user["id"], "task": asyncio.create_task(work), "at": now, "progress": progress}
    finally:
        claude.PROGRESS.reset(token)
    return {"job": job}


@app.get("/api/jobs/{job}")
async def job_result(job: str, user: User):
    found = _jobs.get(job)
    if not found or found["user"] != user["id"]:
        raise HTTPException(404, "Deze analyse is niet meer beschikbaar. Probeer opnieuw.")
    task = found["task"]
    if not task.done():
        return {"done": False, "progress": found["progress"]}
    if task.cancelled():
        raise HTTPException(503, "De analyse werd onderbroken. Probeer opnieuw.")
    if (e := task.exception()) is not None:
        if isinstance(e, (HTTPException, claude.ClaudeError, weather.WeatherError)):
            raise e
        print(f"analysis failed: {e!r}")
        raise HTTPException(500, "Er liep iets mis bij de analyse. Probeer opnieuw.")
    return {"done": True, "result": task.result()}


@app.post("/api/identify")
async def identify(user: AIUser, request: Request, photo: UploadFile | None = File(None), hint: str = Form("")):
    """Recognises a piece from a photo and/or a description. The photo is kept briefly so the new piece can use it."""
    raw = await photo.read() if photo else b""  # now: the upload is gone once this request ends
    if not raw and not hint.strip():
        raise HTTPException(400, "Kies een foto of beschrijf het kledingstuk.")
    return await _background(request, user, _identify(user, raw, hint))


async def _identify(user: dict, raw: bytes, hint: str):
    token = upload = None
    if raw:
        upload = _jpeg(raw, 1568, 80)
        token = db.new_id().replace("-", "")
        (db.TMP_DIR / f"{token}.jpg").write_bytes(_jpeg(raw, 1200, 78))
    return {"info": await claude.identify_item(user, upload, hint.strip() or None), "photo_token": token}


class StylistIn(BaseModel):
    query: str
    use_weather: bool = True


@app.post("/api/stylist")
async def stylist(body: StylistIn, user: AIUser, request: Request):
    query = body.query.strip()[:1000]
    if not query:
        raise HTTPException(400, "Waarvoor zoek je een outfit?")
    return await _background(request, user, _stylist(user, query, body.use_weather))


async def _stylist(user: dict, query: str, use_weather: bool):
    settings = auth.user_settings(user)
    forecast, weather_text = None, ""
    if use_weather and (place := settings.get("place")):
        claude.report(step="Weer opvragen")
        try:
            forecast = await weather.forecast(place)
            weather_text = weather.describe(forecast)
        except weather.WeatherError:
            pass  # without the weather the stylist still works; the app says the weather is missing
    with db.connect() as conn:
        items = [db.item_row(r) for r in conn.execute("SELECT * FROM items WHERE user_id = ?", (user["id"],))]
        worn = [db.worn_row(r) for r in conn.execute("SELECT * FROM worn WHERE user_id = ?", (user["id"],))]
    result = await claude.suggest_outfits(user, query, items, worn, weather=weather_text, profile=settings.get("profile") or "")
    return {**result, "weather": forecast}


# MARK: - Photos

THUMB_SIDE = 480


def _thumb(file: Path) -> Path:
    """A small copy for the grid, made the first time it is asked for and again when the photo changes."""
    thumb = db.PHOTO_DIR / "thumbs" / file.name
    made = file.stat().st_mtime
    if not thumb.exists() or thumb.stat().st_mtime != made:
        thumb.parent.mkdir(exist_ok=True)
        tmp = thumb.with_suffix(".tmp")
        tmp.write_bytes(_jpeg(file.read_bytes(), THUMB_SIDE, 78))
        os.utime(tmp, (made, made))
        tmp.replace(thumb)
    return thumb


@app.get("/photos/{name}")
def photo(name: str, user: User, w: int = 0):
    """Photos are only served to the account they belong to. With w, the small copy for lists."""
    stem = name.removesuffix(".jpg")
    if not name.endswith(".jpg") or not SAFE_ID.fullmatch(stem):
        raise HTTPException(404, "Foto niet gevonden.")
    with db.connect() as conn:
        ok = conn.execute("SELECT 1 FROM items WHERE id = ? AND user_id = ?", (stem, user["id"])).fetchone()
    file = db.PHOTO_DIR / f"{stem}.jpg"
    if not ok or not file.exists():
        raise HTTPException(404, "Foto niet gevonden.")
    if w:
        try:
            file = _thumb(file)
        except HTTPException:
            pass
    return FileResponse(file, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=31536000, immutable"})


# MARK: - Daily maintenance: a copy of the database, old temporary photos and log entries away

def _snapshot() -> None:
    target = db.BACKUP_DIR / f"dressing-{datetime.now(timezone.utc):%Y-%m-%d}.db"
    if target.exists():
        return
    with closing(sqlite3.connect(db.DB_PATH)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)
    for old in sorted(db.BACKUP_DIR.glob("dressing-*.db"))[:-BACKUPS_TO_KEEP]:
        old.unlink(missing_ok=True)


def _maintenance() -> None:
    _snapshot()
    cutoff = time.time() - 24 * 3600
    for tmp in db.TMP_DIR.glob("*.jpg"):
        if tmp.stat().st_mtime < cutoff:
            tmp.unlink(missing_ok=True)
    audit.prune()


async def _daily_maintenance():
    while True:
        try:
            await asyncio.to_thread(_maintenance)
        except Exception as e:  # never let the loop die
            print(f"maintenance failed: {e!r}")
        await asyncio.sleep(6 * 3600)


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
