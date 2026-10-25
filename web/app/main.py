"""Dressing server: JSON API + the installable web app."""

import asyncio
import base64
import html
import http.client
import io
import ipaddress
import json
import math
import os
import re
import secrets
import shutil
import socket
import ssl
import sqlite3
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager, closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import pypdf
from PIL import Image, ImageOps
from pydantic import BaseModel

from . import audit, auth, claude, costs, db, gpt, keys, push, trends, weather

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
BACKUPS_TO_KEEP = 14


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    tasks = [asyncio.create_task(_daily_maintenance()), asyncio.create_task(_job_cleanup()),
             asyncio.create_task(push.question_loop()), asyncio.create_task(_trend_refresh_loop())]
    yield
    for task in tasks:
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
MAX_IMAGE_PIXELS = 24_000_000
UPLOADS = {"/api/identify", "/api/order/pdf", "/api/order/screenshots", "/api/inspiration"}


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
        if img.width * img.height > MAX_IMAGE_PIXELS:
            raise HTTPException(413, "Deze afbeelding heeft te veel pixels. Verklein ze en probeer opnieuw.")
        img = ImageOps.exif_transpose(img).convert("RGB")
    except HTTPException:
        raise
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
        items += [r["id"] for r in conn.execute("SELECT id FROM inspiration WHERE user_id = ?", (user_id,))]
        for table in ("worn", "rejected", "items", "inspiration", "sessions", "ai_usage", "request_keys", "push_subscriptions"):
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
                       "default_model": _service(name).DEFAULT_MODEL, "helper_model": _service(name).HELPER_MODEL}
                for name, spec in keys.PROVIDERS.items()}
    return {"provider": keys.provider(user), "services": services, "quality": claude.quality(user),
            "can_draw": keys.active_key(user, "openai")[0] is not None, "image_model": gpt.IMAGE_MODEL,
            "complex_model": gpt.COMPLEX_MODEL, "review_model": gpt.REVIEW_MODEL,
            "routing": gpt.routing(user), "routes": gpt.routes(user)}


@app.get("/api/state")
def state(user: User):
    settings = auth.user_settings(user)
    with db.connect() as conn:
        return {
            "user": user,
            "items": [db.item_row(r) for r in conn.execute("SELECT * FROM items WHERE user_id = ? ORDER BY date_added DESC", (user["id"],))],
            "worn": [db.worn_row(r) for r in conn.execute("SELECT * FROM worn WHERE user_id = ? ORDER BY date DESC", (user["id"],))],
            "inspiration": [db.inspiration_row(r) for r in _inspiration(conn, user, newest_first=True)],
            "rejected": [db.rejected_row(r) for r in _rejected(conn, user)],
            "categories": [{"key": k, "label": v} for k, v in db.CATEGORIES.items()],
            "statuses": [{"key": k, "label": v} for k, v in db.STATUSES.items()],
            "seasons": db.SEASONS, "formality": db.FORMALITY, "warmth": db.WARMTH, "reject_reasons": REJECT_REASONS,
            "ai": _ai_info(user),
            "ai_usage": costs.overview(conn, user),
            "place": settings.get("place"),
            "trip": settings.get("trip"),
            "profile": settings.get("profile") or "",
            "style_profile": settings.get("style_profile") or "",
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


class QualityIn(BaseModel):
    quality: str


@app.put("/api/settings/quality")
def set_quality(body: QualityIn, user: User):
    """Zuinig or standaard: whether recognising a piece and reading an order use the cheap helper model."""
    if body.quality not in claude.QUALITY:
        raise HTTPException(400, "Kies zuinig of standaard.")
    auth.put_user_setting(user, "quality", body.quality)
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


class RoutingIn(BaseModel):
    routing: str


@app.put("/api/settings/routing")
def set_routing(body: RoutingIn, user: User):
    """ChatGPT on Luna: may the stylist call in Sol and Astra by itself ("auto"), or does Luna do it all ("fixed")."""
    if body.routing not in gpt.ROUTING:
        raise HTTPException(400, "Kies automatisch of vast model.")
    auth.put_user_setting(user, "openai_routing", body.routing)
    return _ai_info(user)


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

REQUEST_KEY = re.compile(r"[A-Za-z0-9-]{8,64}")
REQUEST_KEY_DAYS = 2


def _once(user: dict, key: str, work):
    """Runs a save that the app sent with its own id once: the same id again (a second tap, a retry after an answer
    that got lost) gets the first one's answer, also while the first is still busy. Without an id it just runs."""
    if not REQUEST_KEY.fullmatch(key or ""):
        return work()
    for _ in range(100):  # up to ten seconds for a first one that is still busy
        with db.connect() as conn:
            try:
                conn.execute("INSERT INTO request_keys (user_id, key, at) VALUES (?, ?, ?)", (user["id"], key, db.now_iso()))
                break  # ours: do it
            except sqlite3.IntegrityError:
                row = conn.execute("SELECT result FROM request_keys WHERE user_id = ? AND key = ?", (user["id"], key)).fetchone()
        if row and row["result"] is not None:
            return json.loads(row["result"])
        time.sleep(0.1)  # the first one is busy (or just failed and gave its id back)
    else:
        raise HTTPException(409, "Dit wordt al bewaard.")
    try:
        result = work()
    except BaseException:
        with db.connect() as conn:  # failed: the id is free again, a retry may do it
            conn.execute("DELETE FROM request_keys WHERE user_id = ? AND key = ?", (user["id"], key))
        raise
    with db.connect() as conn:
        conn.execute("UPDATE request_keys SET result = ? WHERE user_id = ? AND key = ?", (json.dumps(result, default=str), user["id"], key))
    return result


class ItemIn(BaseModel):
    data: dict
    photo_token: str = ""
    request_id: str = ""  # the app's id for this save (see _once)


@app.post("/api/items")
def create_item(body: ItemIn, user: User):
    return _once(user, body.request_id, lambda: _create_item(body, user))


def _create_item(body: ItemIn, user: dict) -> dict:
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
    jpeg = await asyncio.to_thread(_jpeg, raw, 1200, 78)
    with db.connect() as conn:
        _get_item(conn, item_id, user)
        _remove_photo(item_id)
        (db.PHOTO_DIR / f"{item_id}.jpg").write_bytes(jpeg)
        conn.execute("UPDATE items SET has_photo = 1 WHERE id = ?", (item_id,))
        return db.item_row(_get_item(conn, item_id, user))


# MARK: - Worn (outfits that were worn)

def _learned_outfit_profile(worn: list[dict], rejected: list[dict]) -> str:
    """Cheap local preference learning; capped and deterministic, with no model/API call."""
    if not worn and not rejected:
        return ""
    from collections import Counter
    good_items, poor_items, good_seasons, good_occasions, reject_reasons = Counter(), Counter(), Counter(), Counter(), Counter()
    good_temps = []
    for log in worn:
        snapshots = {x.get("id"): x for x in log.get("item_snapshots", []) if isinstance(x, dict)}
        names = [snapshots.get(i, {}).get("name") or snapshots.get(i, {}).get("kind") for i in log.get("item_ids", [])]
        names = [name for name in names if name]
        score = log.get("rating")
        if score and score >= 4:
            good_items.update(names)
            if log.get("season"): good_seasons.update([log["season"]])
            if log.get("occasion"): good_occasions.update([log["occasion"]])
            if isinstance(log.get("temperature"), (int, float)): good_temps.append(log["temperature"])
        elif score and score <= 2:
            poor_items.update(names)
    for log in rejected:
        reject_reasons.update(r for r in log.get("reasons", []) if r)
    parts = [f"{len(worn)} gelogde outfits"] if worn else []
    if good_items:
        parts.append("goed beoordeeld: " + ", ".join(f"{n} ({c}×)" for n, c in good_items.most_common(4)))
    if poor_items:
        parts.append("laag beoordeeld: " + ", ".join(f"{n} ({c}×)" for n, c in poor_items.most_common(3)))
    if good_seasons:
        parts.append("goede outfits in " + ", ".join(f"{s} ({c}×)" for s, c in good_seasons.most_common(3)))
    if good_occasions:
        parts.append("goed beoordeeld voor " + ", ".join(f"{o} ({c}×)" for o, c in good_occasions.most_common(3)))
    if good_temps:
        temperature_range = f"{min(good_temps):g} °C" if min(good_temps) == max(good_temps) else f"{min(good_temps):g}–{max(good_temps):g} °C"
        parts.append(f"goed beoordeeld bij {temperature_range}")
    if reject_reasons:
        parts.append("afwijzingsfeedback: " + ", ".join(f"{r} ({c}×)" for r, c in reject_reasons.most_common(3)))
    return "; ".join(parts)[:700]


def _refresh_outfit_learning(user_id: str) -> None:
    with db.connect() as conn:
        worn = [db.worn_row(r) for r in conn.execute("SELECT * FROM worn WHERE user_id = ? ORDER BY date DESC", (user_id,))]
        rejected = [db.rejected_row(r) for r in _rejected(conn, {"id": user_id})]
    auth.put_user_setting({"id": user_id}, "learned_profile", _learned_outfit_profile(worn, rejected) or None)


def _refresh_all_outfit_learning() -> None:
    with db.connect() as conn:
        user_ids = [r["id"] for r in conn.execute("SELECT id FROM users")]
    for user_id in user_ids:
        _refresh_outfit_learning(user_id)


class WornIn(BaseModel):
    item_ids: list[str]
    date: str = ""
    occasion: str = ""
    temperature: float | None = None
    note: str = ""
    rating: int | None = None
    request_id: str = ""  # the app's id for this save (see _once)


def _worn_data(conn, body: WornIn, user: dict, previous_snapshots: list[dict] | None = None) -> tuple[str, dict]:
    previous = {snap.get("id"): snap for snap in previous_snapshots or [] if isinstance(snap, dict) and snap.get("id")}
    rows = {row["id"]: row for row in conn.execute("SELECT id, data FROM items WHERE user_id = ?", (user["id"],))}
    ids = list(dict.fromkeys(i for i in body.item_ids if i in rows or i in previous))
    if not ids:
        raise HTTPException(400, "Kies minstens één kledingstuk.")
    day = body.date.strip() or date.today().isoformat()
    try:
        worn_date = date.fromisoformat(day[:10])
    except ValueError:
        raise HTTPException(400, "Geef een geldige datum.")
    temperature = body.temperature
    if temperature is not None and (not isinstance(temperature, (int, float)) or not math.isfinite(temperature) or not -80 <= temperature <= 70):
        raise HTTPException(400, "Geef een geldige temperatuur tussen -80 en 70 °C.")
    rating = body.rating if body.rating and 1 <= body.rating <= 5 else None
    snapshots = []
    for item_id in ids:
        if item_id in previous:
            snapshots.append(previous[item_id])
        else:
            item = json.loads(rows[item_id]["data"])
            snapshots.append({key: item[key] for key in ("name", "category", "kind", "colors", "brand") if key in item} | {"id": item_id})
    return day[:10], {"item_ids": ids, "item_snapshots": snapshots, "season": claude.season(worn_date),
                      "temperature": round(float(temperature), 1) if temperature is not None else None,
                      "occasion": body.occasion.strip()[:200],
                      "note": body.note.strip()[:2000], "rating": rating}


@app.post("/api/worn")
def add_worn(body: WornIn, user: User):
    return _once(user, body.request_id, lambda: _add_worn(body, user))


def _add_worn(body: WornIn, user: dict) -> dict:
    with db.connect() as conn:
        day, data = _worn_data(conn, body, user)
        push.ask_later(conn, user["id"], day, data)  # "Hoe viel je outfit?" this evening, without a score
        log_id = db.new_id()
        conn.execute("INSERT INTO worn (id, user_id, date, data) VALUES (?, ?, ?, ?)", (log_id, user["id"], day, json.dumps(data)))
        result = db.worn_row(conn.execute("SELECT * FROM worn WHERE id = ?", (log_id,)).fetchone())
    _refresh_outfit_learning(user["id"])
    return result


@app.put("/api/worn/{log_id}")
def update_worn(log_id: str, body: WornIn, user: User):
    with db.connect() as conn:
        row = conn.execute("SELECT data FROM worn WHERE id = ? AND user_id = ?", (log_id, user["id"])).fetchone()
        if not row:
            raise HTTPException(404, "Outfit niet gevonden.")
        old_data = json.loads(row["data"])
        if body.temperature is None and old_data.get("temperature") is not None:
            body = body.model_copy(update={"temperature": old_data["temperature"]})
        day, data = _worn_data(conn, body, user, old_data.get("item_snapshots"))
        conn.execute("UPDATE worn SET date = ?, data = ? WHERE id = ?", (day, json.dumps(data), log_id))
        result = db.worn_row(conn.execute("SELECT * FROM worn WHERE id = ?", (log_id,)).fetchone())
    _refresh_outfit_learning(user["id"])
    return result


@app.delete("/api/worn/{log_id}")
def delete_worn(log_id: str, user: User):
    with db.connect() as conn:
        if not conn.execute("DELETE FROM worn WHERE id = ? AND user_id = ?", (log_id, user["id"])).rowcount:
            raise HTTPException(404, "Outfit niet gevonden.")
    _refresh_outfit_learning(user["id"])
    return {"ok": True}


# MARK: - Rejected (suggested outfits that weren't right, and why)

REJECT_REASONS = ["Te formeel", "Te sportief", "Te warm", "Te koud", "Kleuren botsen", "Combineer ik nooit",
                  "Past niet bij de gelegenheid", "Draag ik niet graag"]
REJECTED_KEPT = 100  # older ones go: tastes change, and the stylist only sees the latest


def _rejected(conn, user: dict, limit: int = REJECTED_KEPT) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM rejected WHERE user_id = ? ORDER BY at DESC, rowid DESC LIMIT ?", (user["id"], limit)).fetchall()


class RejectedIn(BaseModel):
    item_ids: list[str]
    reasons: list[str] = []
    note: str = ""
    query: str = ""
    title: str = ""
    request_id: str = ""  # the app's id for this save (see _once)


@app.post("/api/rejected")
def add_rejected(body: RejectedIn, user: User):
    return _once(user, body.request_id, lambda: _add_rejected(body, user))


def _add_rejected(body: RejectedIn, user: dict) -> dict:
    reasons = list(dict.fromkeys(r for r in body.reasons if r in REJECT_REASONS))
    note = body.note.strip()[:300]
    if not reasons and not note:
        raise HTTPException(400, "Zeg kort wat er niet goed aan was.")
    with db.connect() as conn:
        ids = list(dict.fromkeys(i for i in body.item_ids if i in _own_item_ids(conn, user)))
        if not ids:
            raise HTTPException(400, "Dit outfit bestaat niet meer.")
        data = {"item_ids": ids, "reasons": reasons, "note": note, "query": body.query.strip()[:300], "title": body.title.strip()[:120]}
        rejected_id = db.new_id()
        conn.execute("INSERT INTO rejected (id, user_id, at, data) VALUES (?, ?, ?, ?)", (rejected_id, user["id"], db.now_iso(), json.dumps(data)))
        conn.execute("DELETE FROM rejected WHERE user_id = ? AND id NOT IN (SELECT id FROM rejected WHERE user_id = ? "
                     "ORDER BY at DESC, rowid DESC LIMIT ?)", (user["id"], user["id"], REJECTED_KEPT))
        result = db.rejected_row(conn.execute("SELECT * FROM rejected WHERE id = ?", (rejected_id,)).fetchone())
    _refresh_outfit_learning(user["id"])
    return result


@app.delete("/api/rejected/{rejected_id}")
def delete_rejected(rejected_id: str, user: User):
    """Forgotten: the stylist may suggest this outfit again."""
    with db.connect() as conn:
        if not conn.execute("DELETE FROM rejected WHERE id = ? AND user_id = ?", (rejected_id, user["id"])).rowcount:
            raise HTTPException(404, "Deze afkeuring bestaat niet meer.")
    _refresh_outfit_learning(user["id"])
    return {"ok": True}


# MARK: - Claude
#
# Decoding, scaling and cutting photos takes a while for big ones, so it runs in a thread (asyncio.to_thread): the
# server keeps answering the app in the meantime, also the "is it done yet?" of an analysis that is running.
#
# Analyses run in the background when the app asks (?background=1): it gets a job number at once and asks every few
# seconds whether the answer is there. A phone never holds one request open for minutes, which Safari (and a
# connection that drops while the phone sleeps) breaks off.

_jobs: dict[str, dict] = {}
JOB_SECONDS = 30 * 60
MAX_JOBS = 128


def _clean_jobs(now: float | None = None, for_new_job: bool = False) -> None:
    now = time.monotonic() if now is None else now
    for job, record in list(_jobs.items()):
        if now - record["at"] > JOB_SECONDS:
            _jobs.pop(job, None)
            if not record["task"].done():
                record["task"].cancel()
    if for_new_job:
        while len(_jobs) >= MAX_JOBS:
            oldest, record = min(_jobs.items(), key=lambda pair: pair[1]["at"])
            _jobs.pop(oldest, None)
            if not record["task"].done():
                record["task"].cancel()


async def _job_cleanup():
    while True:
        _clean_jobs()
        await asyncio.sleep(60)


async def _trend_refresh_loop():
    """Refresh the shared fashion trend cache in the background at most every thirty days."""
    cache_file = db.DATA_DIR / "fashion-trends.json"
    trends.restore(cache_file)
    await asyncio.to_thread(_refresh_all_outfit_learning)
    await asyncio.sleep(5)  # let the app start immediately; the current cache is used meanwhile
    while True:
        def fetch(url: str) -> bytes | None:
            result = _safe_get(url, 500_000, timeout=12)
            if not result or result[0] != 200 or len(result[2]) > 500_000:
                return None
            return result[2]
        try:
            await asyncio.to_thread(trends.refresh, cache_file, fetch)
        except Exception as e:
            print(f"trend refresh failed: {e!r}")
        await asyncio.sleep(trends.REFRESH_SECONDS)


async def _analysis(user: dict, task: str, work):
    """Runs one AI analysis under a cost meter: every API call it makes is kept as one analysis, also when it fails
    (those tokens are billed too). Its result gets "analysis" with the estimated cost."""
    calls: list[costs.Call] = []
    token = costs.METER.set(calls)  # here, inside the task that runs the analysis: its own meter
    try:
        result = await work
    except BaseException:
        costs.store(user, task, calls, failed=True)
        raise
    finally:
        costs.METER.reset(token)
    analysis = costs.store(user, task, calls)
    return {**result, "analysis": analysis} if isinstance(result, dict) and analysis else result


async def _background(request: Request, user: dict, work):
    if request.query_params.get("background") != "1":
        return await work
    now = time.monotonic()
    _clean_jobs(now, for_new_job=True)
    job, progress = secrets.token_urlsafe(18), {}
    token = claude.PROGRESS.set(progress)  # the task takes this along: what the analysis reports lands here
    try:
        _jobs[job] = {"user": user["id"], "task": asyncio.create_task(work), "at": now, "progress": progress}
    finally:
        claude.PROGRESS.reset(token)
    return {"job": job}


@app.get("/api/jobs/{job}")
async def job_result(job: str, user: User):
    _clean_jobs()
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
    return await _background(request, user, _analysis(user, "herkennen", _identify(user, raw, hint)))


def _keep_photo(raw: bytes) -> tuple[bytes, str]:
    """The photo as the AI sees it, and a token for the copy kept for the new piece."""
    upload, token = _jpeg(raw, 1568, 80), db.new_id().replace("-", "")
    (db.TMP_DIR / f"{token}.jpg").write_bytes(_jpeg(raw, 1200, 78))
    return upload, token


async def _identify(user: dict, raw: bytes, hint: str):
    token = upload = None
    if raw:
        upload, token = await asyncio.to_thread(_keep_photo, raw)
    try:
        return {"info": await claude.identify_item(user, upload, hint.strip() or None), "photo_token": token}
    except BaseException:
        if token:
            (db.TMP_DIR / f"{token}.jpg").unlink(missing_ok=True)
        raise


class OrderIn(BaseModel):
    text: str
    photos: list[str] = []


MAX_ORDER_PHOTOS = 40


@app.post("/api/order")
async def read_order(body: OrderIn, user: AIUser, request: Request):
    """The pieces in a pasted order confirmation; the product photos it showed (the app sends their addresses, as
    [foto N] in the text) are fetched and kept briefly, as with recognising one piece."""
    text = body.text.strip()[:40000]
    if len(text) < 20:
        raise HTTPException(400, "Plak de volledige bestelmail.")
    urls = [u for u in body.photos[:MAX_ORDER_PHOTOS] if isinstance(u, str)]

    async def fetched_then_read():
        claude.report(step="Foto's ophalen")
        got = await asyncio.gather(*(asyncio.to_thread(_fetch_photo, u) for u in urls))
        return await _read_order(user, text, {n: jpeg for n, jpeg in enumerate(got, 1) if jpeg})
    return await _background(request, user, _analysis(user, "bestelling", fetched_then_read()))


@app.post("/api/order/pdf")
async def read_order_pdf(user: AIUser, request: Request, pdf: UploadFile = File(...)):
    """The same from the mail saved or printed as PDF: its text, and the pictures in it in their order."""
    raw = await pdf.read()
    text, photos = await asyncio.to_thread(_pdf_contents, raw)
    if len(text.strip()) < 20:
        raise HTTPException(400, "In deze PDF staat geen leesbare tekst. Kies de PDF van de bestelmail.")
    return await _background(request, user, _analysis(user, "bestelling", _read_order(user, text[:40000], photos)))


MAX_SHOTS = 6
SHOT_SIDE = 1568  # the longest side the AI sees; the photos are cut from the sharper original


@app.post("/api/order/screenshots")
async def read_order_screenshots(user: AIUser, request: Request, shots: list[UploadFile] = File(...)):
    """The same from screenshots of the mail, or of the order in the shop's app or website; each piece's photo is
    cut out of the screenshot."""
    if len(shots) > MAX_SHOTS:
        raise HTTPException(400, f"Kies hoogstens {MAX_SHOTS} screenshots.")
    raws = [await shot.read() for shot in shots]
    pixels = 0
    for raw in raws:
        try:
            image = Image.open(io.BytesIO(raw))
            pixels += image.width * image.height
        except Exception:
            raise HTTPException(400, "Een screenshot kon niet als foto gelezen worden.")
        if pixels > MAX_IMAGE_PIXELS:
            raise HTTPException(413, "Deze screenshots bevatten samen te veel pixels. Kies minder of kleinere afbeeldingen.")
    originals = await asyncio.to_thread(lambda: [Image.open(io.BytesIO(_jpeg(raw, 3000, 90))) for raw in raws])
    return await _background(request, user, _analysis(user, "bestelling", _read_screenshots(user, originals)))


def _shots_to_send(originals: list[Image.Image]) -> list[tuple[bytes, tuple[int, int]]]:
    sent = []
    for img in originals:
        small = img.copy()
        small.thumbnail((SHOT_SIDE, SHOT_SIDE))
        out = io.BytesIO()
        small.save(out, "JPEG", quality=82)
        sent.append((out.getvalue(), small.size))
    return sent


def _cut(originals: list[Image.Image], sent: list, n: int, box: list[int]) -> bytes:
    """The part of screenshot n inside box (in pixels of the copy the AI saw), cut from the sharp original."""
    x0, y0, x1, y1 = box
    img, scale = originals[n - 1], originals[n - 1].width / sent[n - 1][1][0]
    out = io.BytesIO()
    img.crop((round(x0 * scale), round(y0 * scale), round(x1 * scale), round(y1 * scale))).save(out, "JPEG", quality=85)
    return _jpeg(out.getvalue(), 1200, 80)


def _cut_out(found: dict, originals: list[Image.Image], sent: list) -> None:
    """Each piece's photo, cut from the sharp original where the AI says it is on the screenshot."""
    for piece in found["pieces"]:
        if crop := piece.pop("crop", None):
            piece["jpeg"] = _cut(originals, sent, *crop)


async def _read_screenshots(user: dict, originals: list[Image.Image]):
    sent = await asyncio.to_thread(_shots_to_send, originals)
    found = await claude.read_order(user, "", {}, sent)
    await asyncio.to_thread(_cut_out, found, originals, sent)
    return await asyncio.to_thread(_order_result, found, {})


def _pdf_contents(raw: bytes) -> tuple[str, dict[int, bytes]]:
    try:
        reader = pypdf.PdfReader(io.BytesIO(raw))
        pages = list(reader.pages)[:10]
    except Exception:
        raise HTTPException(400, "Dit bestand kon niet als PDF gelezen worden.")
    text, photos = [], {}
    pixel_budget = MAX_IMAGE_PIXELS
    for page in pages:
        try:
            text.append(page.extract_text() or "")
        except Exception:
            continue
        try:
            images = list(page.images)
        except Exception:
            images = []
        for image in images:
            if len(photos) >= MAX_ORDER_PHOTOS:
                break
            try:
                source = Image.open(io.BytesIO(image.data))
                pixels = source.width * source.height
                if pixels > pixel_budget:
                    continue
                pixel_budget -= pixels
                size = source.size
                if min(size) >= 12:  # colour swatches count: the AI reads the colour from them
                    photos[len(photos) + 1] = _jpeg(image.data, 1200, 85)
            except Exception:
                continue
    return "\n".join(text), photos


def _public_host(url: str) -> bool:
    """Only https to an address on the internet: never this server, the tailnet or the home network."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return False
    try:
        found = socket.getaddrinfo(parts.hostname, parts.port or 443, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError, ValueError):
        return False
    return bool(found) and all(ipaddress.ip_address(f[4][0]).is_global for f in found)


def _public_addresses(url: str) -> tuple[urllib.parse.SplitResult, list[str]] | None:
    """Resolve and validate once; the same address list is used by the socket connection."""
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            return None
        resolved = socket.getaddrinfo(parts.hostname, parts.port or 443, type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(ipaddress.ip_address(row[4][0]).compressed for row in resolved))
    except (OSError, UnicodeError, ValueError):
        return None
    return (parts, addresses) if addresses and all(ipaddress.ip_address(ip).is_global for ip in addresses) else None


def _safe_get(url: str, max_bytes: int, timeout: int = 10, image_max_bytes: int | None = None) -> tuple[int, object, bytes, str] | None:
    """HTTPS GET with DNS-pinned public IPs and the original hostname for TLS and Host validation."""
    current = url
    for _ in range(6):
        resolved = _public_addresses(current)
        if not resolved:
            return None
        parts, addresses = resolved
        host, port = parts.hostname, parts.port or 443
        path = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
        conn = None
        try:
            class PinnedHTTPSConnection(http.client.HTTPSConnection):
                def connect(self):
                    last_error = None
                    for address in addresses:
                        try:
                            sock = socket.create_connection((address, port), timeout=self.timeout)
                            self.sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
                            return
                        except OSError as exc:
                            last_error = exc
                    if last_error:
                        raise last_error

            conn = PinnedHTTPSConnection(host, port, timeout=timeout, context=ssl.create_default_context())
            conn.request("GET", path, headers={"User-Agent": "Mozilla/5.0 (compatible; Dressing)",
                                                "Accept": "image/*, text/html, application/rss+xml, text/xml"})
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location:
                    return response.status, response.headers, b"", current
                current = urllib.parse.urljoin(current, location)
                continue
            limit = image_max_bytes if image_max_bytes is not None and response.getheader("Content-Type", "").startswith("image/") else max_bytes
            body = response.read(limit + 1)
            return response.status, response.headers, body, current
        except (OSError, ValueError, ssl.SSLError, http.client.HTTPException):
            return None
        finally:
            if conn:
                conn.close()
    return None


def _fetch_photo(url: str) -> bytes | None:
    """A picture from a web shop's mail, or None when it can't be had (gone, not a picture, too large)."""
    result = _safe_get(url, MAX_UPLOAD, timeout=10)
    if not result:
        return None
    status, headers, data, _ = result
    if status != 200 or not headers.get("Content-Type", "").startswith("image/"):
        return None
    if len(data) > MAX_UPLOAD:
        return None
    try:
        return _jpeg(data, 1200, 78)
    except HTTPException:
        return None


PAGE_BYTES = 3_000_000
OG_IMAGE = re.compile(r"""<meta\b[^>]*?(?:property|name)\s*=\s*["'](?:og:image(?::secure_url)?|twitter:image)["'][^>]*>""", re.I)
CONTENT = re.compile(r"""\bcontent\s*=\s*["']([^"']+)["']""", re.I)


def _fetch_page_photo(url: str) -> bytes | None:
    """The picture behind a link: the link itself when it is a picture, else the page's preview picture (og:image), as
    a Pinterest pin has. None when there is none, or the page hides it (Instagram, without signing in)."""
    result = _safe_get(url, PAGE_BYTES, timeout=10, image_max_bytes=MAX_UPLOAD)
    if not result:
        return None
    status, headers, data, final = result
    if status != 200:
        return None
    kind = headers.get("Content-Type", "")
    if kind.startswith("image/"):
        try:
            return _jpeg(data, 3000, 90) if len(data) <= MAX_UPLOAD else None
        except HTTPException:
            return None
    page = data.decode("utf-8", "replace")
    for tag in OG_IMAGE.findall(page):
        if found := CONTENT.search(tag):
            if photo := _fetch_photo(urllib.parse.urljoin(final, html.unescape(found.group(1)))):
                return photo
    return None


def _big_enough(jpeg: bytes) -> bool:
    """A product photo, not a colour swatch the AI picked by mistake."""
    return min(Image.open(io.BytesIO(jpeg)).size) >= 40


async def _read_order(user: dict, text: str, photos: dict[int, bytes]):
    small = await asyncio.to_thread(lambda: {n: _jpeg(jpeg, 512, 75) for n, jpeg in photos.items()})
    found = await claude.read_order(user, text, small)
    return await asyncio.to_thread(_order_result, found, photos)


def _order_result(found: dict, photos: dict[int, bytes]) -> dict:
    """Each piece with its photo kept briefly (a token for saving it) and a small copy to show while choosing."""
    pieces = []
    for p in found["pieces"]:
        token = thumb = None
        if (jpeg := p.get("jpeg") or photos.get(p["photo"])) and _big_enough(jpeg):
            token = db.new_id().replace("-", "")
            (db.TMP_DIR / f"{token}.jpg").write_bytes(jpeg)  # each piece its own copy: the token moves the file
            thumb = "data:image/jpeg;base64," + base64.b64encode(_jpeg(jpeg, 240, 72)).decode()
        pieces.append({"info": p["info"], "photo_token": token, "thumb": thumb})
    return {"shop": found["shop"], "date": found["date"], "pieces": pieces}


# MARK: - Inspiration: outfits from Instagram or Pinterest, and the style profile the AI writes from them

def _inspiration(conn, user: dict, newest_first: bool = False) -> list[sqlite3.Row]:
    order = "DESC" if newest_first else "ASC"
    return conn.execute(f"SELECT * FROM inspiration WHERE user_id = ? ORDER BY added_at {order}, rowid {order}", (user["id"],)).fetchall()


def _look_texts(user: dict) -> list[str]:
    with db.connect() as conn:
        return [json.loads(r["data"]).get("description", "") for r in _inspiration(conn, user)]


BOARD_PINS = 50  # the most pins one import takes; a board's feed holds about the latest 25
NOT_A_BOARD = {"pin", "search", "ideas", "today", "settings", "business", "_", "categories", "explore"}


def _pinterest(host: str) -> bool:
    return bool(re.fullmatch(r"([a-z]{2,3}\.)?(www\.)?pinterest(\.[a-z]{2,3}){1,2}", host or ""))


def _resolve(url: str) -> str:
    """Where a short link (pin.it) leads; any other link as it is."""
    if urllib.parse.urlsplit(url).hostname != "pin.it":
        return url
    result = _safe_get(url, PAGE_BYTES, timeout=10)
    return result[3] if result and result[0] in (200, 301, 302, 303, 307, 308) else url


def _board_feed(url: str) -> str | None:
    """The RSS feed of a Pinterest board (pinterest.com/<name>/<board>/), or None when the link isn't to a board."""
    parts = urllib.parse.urlsplit(url)
    path = [p for p in parts.path.split("/") if p]
    if not _pinterest(parts.hostname or "") or len(path) != 2 or path[0] in NOT_A_BOARD or path[1].endswith(".rss"):
        return None
    return f"https://{parts.hostname}/{path[0]}/{path[1]}.rss"


def _board_pins(feed: str) -> list[tuple[str, str]]:
    """The pins on a public board, newest first: (link to the pin, its picture in the feed)."""
    result = _safe_get(feed, PAGE_BYTES, timeout=15)
    if not result or result[0] != 200:
        return []
    try:
        root = ET.fromstring(result[2])
    except ET.ParseError:
        return []
    pins = []
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        found = re.search(r"""<img[^>]+src=["']([^"']+)["']""", item.findtext("description") or "")
        if link.startswith("https://") and found:
            pins.append((link, html.unescape(found.group(1))))
    return pins[:BOARD_PINS]


def _pin_photo(picture: str) -> bytes | None:
    """A pin's picture: the feed shows a small one (236x), the same is there larger (736x)."""
    larger = re.sub(r"/\d+x/", "/736x/", picture, count=1)
    return (_fetch_photo(larger) if larger != picture else None) or _fetch_photo(picture)


async def _import_board(user: dict, feed: str):
    """Every pin on the board that isn't in Inspiratie yet, 6 at a time to the AI; the profile grows with each batch."""
    claude.report(step="Bord ophalen")
    pins = await asyncio.to_thread(_board_pins, feed)
    if not pins:
        raise HTTPException(400, "Dit bord vond ik niet, of het is geheim. Zet het openbaar op Pinterest, of gebruik screenshots.")
    with db.connect() as conn:
        known = {json.loads(r["data"]).get("url") for r in _inspiration(conn, user)}
    new = [pin for pin in pins if pin[0] not in known]
    result = {"added": [], "skipped": 0, "known": len(pins) - len(new), "pins": len(pins)}
    for start in range(0, len(new), MAX_SHOTS):
        batch = new[start:start + MAX_SHOTS]
        claude.report(step=f"Pins {start + 1}–{start + len(batch)} van {len(new)} ophalen")
        photos = await asyncio.gather(*(asyncio.to_thread(_pin_photo, picture) for _, picture in batch))
        got = [(link, photo) for (link, _), photo in zip(batch, photos) if photo]
        result["skipped"] += len(batch) - len(got)
        if not got:
            continue
        claude.report(step=f"Pins {start + 1}–{start + len(batch)} van {len(new)} bekijken")
        originals = await asyncio.to_thread(lambda: [Image.open(io.BytesIO(photo)) for _, photo in got])
        done = await _read_inspiration(user, originals, [link for link, _ in got])
        result["added"] += done["added"]
        result["skipped"] += done["skipped"]
    result["style_profile"] = auth.user_settings(user).get("style_profile") or ""
    return result


@app.post("/api/inspiration")
async def add_inspiration(user: AIUser, request: Request, shots: list[UploadFile] = File(None), url: str = Form("")):
    """Screenshots of outfits (Instagram, Pinterest), a link to a pin, or a link to a public Pinterest board: the AI
    describes each outfit, cuts it out of a screenshot and writes the style profile anew."""
    raws = [await shot.read() for shot in shots or []]
    url = url.strip()[:2000]
    if len(raws) > MAX_SHOTS:
        raise HTTPException(400, f"Kies hoogstens {MAX_SHOTS} screenshots.")
    if not raws and not url:
        raise HTTPException(400, "Kies screenshots of plak een link.")
    if not raws and not url.startswith(("https://", "http://")):
        raise HTTPException(400, "Dit lijkt geen link. Kopieer de link van de pin (Delen → Link kopiëren).")
    url = re.sub(r"^http://", "https://", url)
    if not raws and (feed := _board_feed(await asyncio.to_thread(_resolve, url))):
        return await _background(request, user, _analysis(user, "inspiratie", _import_board(user, feed)))

    async def work():
        found = raws
        if not found:
            claude.report(step="Foto ophalen")
            if not (photo := await asyncio.to_thread(_fetch_page_photo, url)):
                raise HTTPException(400, "Op deze link vond ik geen foto. Bij Instagram lukt dat meestal niet: neem dan een screenshot.")
            found = [photo]
        originals = await asyncio.to_thread(lambda: [Image.open(io.BytesIO(_jpeg(raw, 3000, 90))) for raw in found])
        return await _read_inspiration(user, originals, [url if not raws else ""] * len(originals))
    return await _background(request, user, _analysis(user, "inspiratie", work()))


async def _read_inspiration(user: dict, originals: list[Image.Image], urls: list[str]):
    sent = await asyncio.to_thread(_shots_to_send, originals)
    profile = auth.user_settings(user).get("style_profile") or ""
    found = await claude.read_inspiration(user, sent, await asyncio.to_thread(_look_texts, user), profile)
    added = await asyncio.to_thread(_keep_looks, user, found["looks"], originals, sent, urls)
    if added and found["profile"]:
        auth.put_user_setting(user, "style_profile", found["profile"])
    return {"added": added, "skipped": len(originals) - len(added), "style_profile": auth.user_settings(user).get("style_profile") or ""}


def _keep_looks(user: dict, looks: list[dict], originals: list[Image.Image], sent: list, urls: list[str]) -> list[dict]:
    """Each outfit with its photo: cut out of the screenshot where the AI says it is, else the whole picture; with the
    link to the pin it came from (urls: one per picture, empty for a screenshot)."""
    added, used = [], set()
    for look in looks:
        n = look["photo"]
        if n in used:  # one outfit per picture
            continue
        used.add(n)
        if look["box"]:
            jpeg = _cut(originals, sent, n, look["box"])
        else:
            out = io.BytesIO()
            originals[n - 1].save(out, "JPEG", quality=85)
            jpeg = _jpeg(out.getvalue(), 1200, 80)
        look_id = db.new_id()
        (db.PHOTO_DIR / f"{look_id}.jpg").write_bytes(jpeg)
        data = {k: look[k] for k in ("description", "colors", "style_tags", "pieces")}
        if urls[n - 1]:
            data["url"] = urls[n - 1]
        with db.connect() as conn:
            conn.execute("INSERT INTO inspiration (id, user_id, added_at, data) VALUES (?, ?, ?, ?)",
                         (look_id, user["id"], db.now_iso(), json.dumps(data)))
            added.append(db.inspiration_row(conn.execute("SELECT * FROM inspiration WHERE id = ?", (look_id,)).fetchone()))
    return added


@app.delete("/api/inspiration/{look_id}")
def delete_inspiration(look_id: str, user: User):
    """The outfit goes; the profile stays as it is until it is written anew (Opnieuw opmaken)."""
    with db.connect() as conn:
        if not conn.execute("DELETE FROM inspiration WHERE id = ? AND user_id = ?", (look_id, user["id"])).rowcount:
            raise HTTPException(404, "Deze inspiratie bestaat niet meer.")
    _remove_photo(look_id)
    return {"ok": True}


@app.post("/api/inspiration/profile")
async def rewrite_style_profile(user: AIUser, request: Request):
    """The style profile written anew from the outfits that are there now, e.g. after removing some."""
    if not await asyncio.to_thread(_look_texts, user):
        raise HTTPException(400, "Bewaar eerst een paar outfits die je mooi vindt.")

    async def work():
        found = await claude.read_inspiration(user, [], await asyncio.to_thread(_look_texts, user), "")
        if found["profile"]:
            auth.put_user_setting(user, "style_profile", found["profile"])
        return {"style_profile": auth.user_settings(user).get("style_profile") or ""}
    return await _background(request, user, _analysis(user, "inspiratie", work()))


@app.put("/api/settings/style-profile")
def set_style_profile(body: ProfileIn, user: User):
    """The profile as the user corrected it; the next outfit that is added builds on this."""
    auth.put_user_setting(user, "style_profile", body.profile.strip()[:1500] or None)
    return {"style_profile": auth.user_settings(user).get("style_profile") or ""}


# MARK: - The suitcase: one trip at a time, with where, when and what is packed

class TripIn(BaseModel):
    destination: str
    start: str
    end: str


def _trip(user: dict) -> dict | None:
    return auth.user_settings(user).get("trip")


@app.put("/api/trip")
async def set_trip(body: TripIn, user: User):
    """Where and when; what is already packed stays in the suitcase."""
    try:
        start, end = date.fromisoformat(body.start[:10]), date.fromisoformat(body.end[:10])
    except ValueError:
        raise HTTPException(400, "Geef de dag van vertrek en van terugkomst.")
    if end < start:
        raise HTTPException(400, "De terugkomst ligt voor het vertrek.")
    if (end - start).days > 92:
        raise HTTPException(400, "Een reis van meer dan drie maanden? Kies kortere data.")
    old = _trip(user) or {}
    name = body.destination.strip()
    if not name:
        raise HTTPException(400, "Waar ga je naartoe?")
    place = old["place"] if old.get("place", {}).get("query") == name else {**await weather.find_place(name), "query": name}
    trip = {"place": place, "start": start.isoformat(), "end": end.isoformat(), "item_ids": old.get("item_ids", [])}
    auth.put_user_setting(user, "trip", trip)
    return {"trip": trip}


class PackedIn(BaseModel):
    item_ids: list[str]


@app.put("/api/trip/items")
def pack(body: PackedIn, user: User):
    trip = _trip(user)
    if not trip:
        raise HTTPException(400, "Zeg eerst waar en wanneer je op reis gaat.")
    with db.connect() as conn:
        own = _own_item_ids(conn, user)
    trip["item_ids"] = list(dict.fromkeys(i for i in body.item_ids if i in own))
    auth.put_user_setting(user, "trip", trip)
    return {"trip": trip}


@app.delete("/api/trip")
def end_trip(user: User):
    auth.put_user_setting(user, "trip", None)
    return {"trip": None}


@app.get("/api/trip/weather")
async def trip_weather(user: User):
    trip = _trip(user)
    if not trip:
        raise HTTPException(404, "Geen reis gepland.")
    return await weather.trip_forecast(trip["place"], trip["start"], trip["end"])


class EarlierOutfit(BaseModel):
    title: str = ""
    item_ids: list[str] = []


class EarlierAnswer(BaseModel):
    """What was asked before and what the stylist suggested, for a follow-up ("andere schoenen")."""
    queries: list[str] = []
    outfits: list[EarlierOutfit] = []


class StylistIn(BaseModel):
    query: str
    use_weather: bool = True
    trip: bool = False  # on the trip: from the suitcase (or, while it is empty, to pack) with the destination's weather
    previous: EarlierAnswer | None = None  # set for a follow-up on the answer the app shows


@app.post("/api/stylist")
async def stylist(body: StylistIn, user: AIUser, request: Request):
    query = body.query.strip()[:1000]
    if not query:
        raise HTTPException(400, "Waarvoor zoek je een outfit?")
    previous = None
    if body.previous and (body.previous.queries or body.previous.outfits):
        previous = {"queries": [q.strip()[:1000] for q in body.previous.queries if q.strip()][-6:],
                    "outfits": [{"title": o.title.strip()[:120], "item_ids": o.item_ids[:12]} for o in body.previous.outfits[:3]]}
    return await _background(request, user, _analysis(user, "stylist", _stylist(user, query, body.use_weather, body.trip, previous)))


async def _stylist(user: dict, query: str, use_weather: bool, on_trip: bool = False, previous: dict | None = None):
    settings = auth.user_settings(user)
    forecast, weather_text, packed = None, "", None
    if on_trip and (trip := settings.get("trip")):
        packed = trip["item_ids"] or None  # an empty suitcase: the stylist helps to pack from the dressing
        claude.report(step="Weer op je bestemming opvragen")
        try:
            weather_text = weather.describe_trip(await weather.trip_forecast(trip["place"], trip["start"], trip["end"]))
        except weather.WeatherError:
            weather_text = f"Ik ben op reis in {trip['place']['name']} van {trip['start']} tot en met {trip['end']}."
        if not packed:
            weather_text += "\nIk moet nog inpakken: stel outfits voor die ik meeneem, die onderling goed combineren."
    elif use_weather and (place := settings.get("place")):
        claude.report(step="Weer opvragen")
        try:
            forecast = await weather.forecast(place)
            weather_text = weather.describe(forecast)
        except weather.WeatherError:
            pass  # without the weather the stylist still works; the app says the weather is missing
    elsewhere: list[dict] = []

    async def weather_at(asked: dict) -> str:
        """The weather where and when the question is about, when that isn't here today or tomorrow."""
        name = str(asked.get("plaats") or "").strip()[:80]
        if not name:
            return "Geef een plaats."
        try:
            first = date.fromisoformat(str(asked.get("van") or "")[:10])
        except ValueError:
            first = date.today()
        try:
            last = min(max(date.fromisoformat(str(asked.get("tot") or "")[:10]), first), first + timedelta(days=13))
        except ValueError:
            last = first
        claude.report(step=f"Weer in {name} opvragen")
        try:
            w = await weather.trip_forecast(await weather.find_place(name), first.isoformat(), last.isoformat())
        except weather.WeatherError as e:
            return f"{e} Ga uit van het gebruikelijke weer in {name} in die periode."
        elsewhere.append(w)
        return weather.describe_days(w)
    with db.connect() as conn:
        items = [db.item_row(r) for r in conn.execute("SELECT * FROM items WHERE user_id = ?", (user["id"],))]
        worn = [db.worn_row(r) for r in conn.execute("SELECT * FROM worn WHERE user_id = ?", (user["id"],))]
        rejected = [db.rejected_row(r) for r in _rejected(conn, user, claude.REJECTED_SHOWN)]
    trend_text, trend_sources = trends.context()
    result = await claude.suggest_outfits(user, query, items, worn, weather=weather_text, profile=settings.get("profile") or "",
                                          style=settings.get("style_profile") or "", packed=packed, trip=on_trip and bool(settings.get("trip")), rejected=rejected, previous=previous, weather_at=weather_at if use_weather and not on_trip else None, trends=trend_text, learned=settings.get("learned_profile") or "")
    return {**result, "weather": forecast, "elsewhere": elsewhere, "trend_sources": trend_sources}


class PictureIn(BaseModel):
    item_ids: list[str]


@app.post("/api/outfit-picture")
async def outfit_picture(body: PictureIn, user: User, request: Request):
    """A picture of someone wearing these pieces, drawn by ChatGPT from their photos. Only this account's pieces, and
    only when asked for one outfit with its button: never by itself."""
    ids = list(dict.fromkeys(i for i in body.item_ids if isinstance(i, str) and SAFE_ID.fullmatch(i)))[:12]
    if not ids:
        raise HTTPException(400, "Kies de stukken van het outfit.")
    with db.connect() as conn:
        rows = {r["id"]: db.item_row(r) for r in conn.execute(
            f"SELECT * FROM items WHERE user_id = ? AND id IN ({', '.join('?' * len(ids))})", (user["id"], *ids))}
    if len(rows) != len(ids):
        raise HTTPException(404, "Een van deze stukken staat niet (meer) in je dressing.")
    return await _background(request, user, _analysis(user, "outfitbeeld", _draw(user, [rows[i] for i in ids])))


def _piece_text(item: dict) -> str:
    parts = [item.get("name") or item.get("kind") or db.CATEGORIES.get(item.get("category"), "stuk")]
    if item.get("colors"):
        parts.append("/".join(item["colors"]))
    parts += [item[k] for k in ("pattern", "material", "fit") if item.get(k)]
    return ", ".join(str(p) for p in parts)


async def _draw(user: dict, items: list[dict]):
    def photo(item: dict) -> bytes | None:
        file = db.PHOTO_DIR / f"{item['id']}.jpg"
        return file.read_bytes() if item.get("photo") and file.exists() else None
    pieces = [(_piece_text(i), await asyncio.to_thread(photo, i)) for i in items]
    claude.report(step="Beeld tekenen")
    jpeg = await gpt.draw_outfit(user, pieces)
    return {"image": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}


# MARK: - Notifications ("Hoe viel je outfit?")

@app.get("/api/push/key")
def push_key(user: User):
    return {"key": push.public_key()}


class PushIn(BaseModel):
    subscription: dict


@app.post("/api/push/subscribe")
def push_subscribe(body: PushIn, user: User, request: Request):
    try:
        push.save(user, body.subscription, auth.device_name(request.headers.get("user-agent", "")))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"devices": push.devices(user)}


class EndpointIn(BaseModel):
    endpoint: str


@app.post("/api/push/unsubscribe")
def push_unsubscribe(body: EndpointIn, user: User):
    push.remove(user, body.endpoint)
    return {"devices": push.devices(user)}


@app.get("/api/push/devices")
def push_devices(user: User):
    return push.devices(user)


@app.delete("/api/push/devices/{did}")
def push_forget_device(did: str, user: User):
    """Stops notifications to one device, e.g. an old installation of the app that no longer exists."""
    if not push.forget(user, did):
        raise HTTPException(404, "Toestel niet gevonden.")
    return {"devices": push.devices(user)}


@app.post("/api/push/test")
async def push_test(user: User):
    message = {"title": "👕 Dressing", "body": "Meldingen staan aan. Draag je een outfit zonder score, dan vragen we 's avonds hoe hij viel.",
               "url": "/"}
    if not await push.send(user["id"], message):
        raise HTTPException(400, "Er is geen toestel met meldingen aan. Zet ze eerst aan op je gsm.")
    return {"ok": True}


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
    """Photos (of a piece or of an inspiration) are only served to the account they belong to. With w, the small copy
    for lists."""
    stem = name.removesuffix(".jpg")
    if not name.endswith(".jpg") or not SAFE_ID.fullmatch(stem):
        raise HTTPException(404, "Foto niet gevonden.")
    with db.connect() as conn:
        ok = conn.execute("SELECT 1 FROM items WHERE id = ? AND user_id = ? UNION ALL "
                          "SELECT 1 FROM inspiration WHERE id = ? AND user_id = ?", (stem, user["id"], stem, user["id"])).fetchone()
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
    with db.connect() as conn:  # a retry comes within seconds; two days is plenty
        cutoff = (datetime.now(timezone.utc) - timedelta(days=REQUEST_KEY_DAYS)).isoformat(timespec="seconds")
        conn.execute("DELETE FROM request_keys WHERE at < ?", (cutoff,))


async def _daily_maintenance():
    while True:
        try:
            await asyncio.to_thread(_maintenance)
        except Exception as e:  # never let the loop die
            print(f"maintenance failed: {e!r}")
        await asyncio.sleep(6 * 3600)


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
