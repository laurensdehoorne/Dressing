"""Accounts: password hashing, login sessions in a cookie, and the FastAPI dependencies that guard the API."""

import base64
import hashlib
import hmac
import json
import secrets
import threading
from datetime import datetime, timedelta, timezone

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import HTTPException, Request, Response

from . import db

COOKIE = "dr_session"
SESSION_DAYS = 30
MIN_PASSWORD = 8
# scrypt cost: ~50 ms and 16 MB per check, enough to make guessing stolen hashes slow.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1
# At most this many scrypt runs at once (the server has ~40 request threads): a flood of sign-ins from the internet
# can't take all the memory and CPU. Who finds no free slot in time is told the server is busy.
SCRYPT_SLOTS, SCRYPT_WAIT = 4, 10
_scrypt_slots = threading.BoundedSemaphore(SCRYPT_SLOTS)
# The brake on wrong passwords is in audit.py, next to the sign-in log.


# MARK: - Passwords

def _scrypt(password: str, salt: bytes, n: int = SCRYPT_N, r: int = SCRYPT_R, p: int = SCRYPT_P, dklen: int = 32) -> bytes:
    if not _scrypt_slots.acquire(timeout=SCRYPT_WAIT):
        raise HTTPException(503, "De server is even druk. Probeer het zo meteen opnieuw.")
    try:
        return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=dklen)
    finally:
        _scrypt_slots.release()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = _scrypt(password, salt)
    b64 = lambda b: base64.b64encode(b).decode()
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${b64(salt)}${b64(digest)}"


DUMMY_HASH = hash_password(secrets.token_urlsafe(16))  # checked when a username doesn't exist, to keep timing equal


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, digest = stored.split("$")
        expected = base64.b64decode(digest)
        actual = _scrypt(password, base64.b64decode(salt), int(n), int(r), int(p), len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def check_new_password(password: str) -> str:
    if len(password) < MIN_PASSWORD:
        raise HTTPException(400, f"Kies een wachtwoord van minstens {MIN_PASSWORD} tekens.")
    if len(password) > 200:
        raise HTTPException(400, "Dit wachtwoord is te lang.")
    return password


def check_username(username: str) -> str:
    name = username.strip()
    if not 1 <= len(name) <= 40:
        raise HTTPException(400, "Kies een gebruikersnaam van 1 tot 40 tekens.")
    return name


# MARK: - Users

def public_user(row) -> dict:
    return {"id": row["id"], "username": row["username"], "is_admin": bool(row["is_admin"]), "created_at": row["created_at"]}


def create_user(conn, username: str, password: str, is_admin: bool = False) -> dict:
    username, password = check_username(username), check_new_password(password)
    if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
        raise HTTPException(400, f"De gebruikersnaam ‘{username}’ bestaat al.")
    user_id = db.new_id()
    conn.execute("INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (?, ?, ?, ?, ?)",
                 (user_id, username, hash_password(password), int(is_admin), db.now_iso()))
    return public_user(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


def set_password(conn, user_id: str, password: str, data_key: bytes | None = None) -> bytes | None:
    """Changes a password and signs that account out everywhere. With the account's unlocked data key (a change by
    the account itself) its secrets move to a new data key, locked with the new password, which is returned: someone
    who knew the old password (say, the administrator who made the account) can't use what they could open then.
    Without it (a reset by the administrator) the vault can't be opened any more, so it and the API key are dropped."""
    check_new_password(password)
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), user_id))
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    data = _settings(conn, user_id)
    new_key = None
    if data_key:
        new_key = secrets.token_bytes(32)
        for name in SECRETS:
            if (blob := data.get(name)) and (plain := decrypt_secret(data_key, blob)):
                data[name] = encrypt_secret(new_key, plain)
            else:
                data.pop(name, None)
        data["vault"] = _lock_vault(new_key, password)
    else:
        for key in ("vault", *SECRETS):
            data.pop(key, None)
    _save_settings(conn, user_id, data)
    return new_key


# MARK: - The vault: secrets (the API key) only the account's own password can open
#
# Each account has a random data key that encrypts its secrets (AES-GCM). The data key itself is stored only
# encrypted with a key derived from the password (scrypt), which the server never keeps. At sign-in the data key is
# unlocked and stored with that session, encrypted with a key derived from the session cookie, which the server
# doesn't keep either (only a different hash of it). So the database and its backups hold no readable API key.

SECRETS = ("api_key_enc", "openai_key_enc")  # settings that hold a secret encrypted with the data key (see keys.py)


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _password_key(password: str, salt: bytes) -> bytes:
    return _scrypt(password, salt)


def _lock_vault(data_key: bytes, password: str) -> dict:
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    return {"salt": _b64(salt), "nonce": _b64(nonce), "key": _b64(AESGCM(_password_key(password, salt)).encrypt(nonce, data_key, b"vault"))}


def _open_vault(vault: dict, password: str) -> bytes:
    key = _password_key(password, base64.b64decode(vault["salt"]))
    return AESGCM(key).decrypt(base64.b64decode(vault["nonce"]), base64.b64decode(vault["key"]), b"vault")


def encrypt_secret(data_key: bytes, text: str) -> dict:
    nonce = secrets.token_bytes(12)
    return {"nonce": _b64(nonce), "data": _b64(AESGCM(data_key).encrypt(nonce, text.encode(), b"secret"))}


def decrypt_secret(data_key: bytes, blob: dict) -> str | None:
    try:
        return AESGCM(data_key).decrypt(base64.b64decode(blob["nonce"]), base64.b64decode(blob["data"]), b"secret").decode()
    except (InvalidTag, KeyError, ValueError, TypeError):
        return None


def _settings(conn, user_id: str) -> dict:
    row = conn.execute("SELECT settings FROM users WHERE id = ?", (user_id,)).fetchone()
    return json.loads(row["settings"]) if row else {}


def _save_settings(conn, user_id: str, data: dict) -> None:
    conn.execute("UPDATE users SET settings = ? WHERE id = ?", (json.dumps(data), user_id))


def unlock(conn, user_id: str, password: str) -> bytes:
    """The account's data key, opened with its (already checked) password; made the first time."""
    data = _settings(conn, user_id)
    try:
        data_key = _open_vault(data["vault"], password) if data.get("vault") else None
    except (InvalidTag, KeyError, ValueError, TypeError):
        # A vault this (correct) password can't open: start a new one rather than refuse the sign-in.
        # What was in the old one is lost; the API keys have to be entered again.
        data_key = None
        for name in SECRETS:
            data.pop(name, None)
    if data_key is None:
        data_key = secrets.token_bytes(32)
        data["vault"] = _lock_vault(data_key, password)
    _save_settings(conn, user_id, data)
    return data_key


def _session_key(token: str) -> bytes:
    """Encrypts the data key stored with a session. Only the cookie gives it: the database keeps a different hash."""
    return hmac.new(token.encode(), b"dressing-session-data-key", hashlib.sha256).digest()


class SessionUser(dict):
    """The signed-in account as sent to the app, plus its unlocked data key, which never leaves the server
    (an attribute, not a dict item, so it isn't part of any response)."""
    data_key: bytes | None = None


def user_settings(user: dict) -> dict:
    with db.connect() as conn:
        row = conn.execute("SELECT settings FROM users WHERE id = ?", (user["id"],)).fetchone()
    return json.loads(row["settings"]) if row else {}


def put_user_setting(user: dict, key: str, value) -> None:
    with db.connect() as conn:
        data = json.loads(conn.execute("SELECT settings FROM users WHERE id = ?", (user["id"],)).fetchone()["settings"])
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
        conn.execute("UPDATE users SET settings = ? WHERE id = ?", (json.dumps(data), user["id"]))


# MARK: - Sessions

def _expiry() -> str:
    """Same format as db.now_iso(), so the two compare correctly as text."""
    return (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _is_https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "") == "https"


def device_name(user_agent: str) -> str:
    """A short, readable name for the device a session belongs to (from the browser's User-Agent)."""
    ua = user_agent or ""
    device = next((name for key, name in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Macintosh", "Mac"),
                                          ("Windows", "Windows"), ("Linux", "Linux")) if key in ua), "Onbekend toestel")
    browser = next((name for key, name in (("Edg/", "Edge"), ("Firefox/", "Firefox"), ("CriOS", "Chrome"), ("Chrome/", "Chrome"),
                                           ("Safari/", "Safari")) if key in ua), "")
    if device in ("iPhone", "iPad") and "Safari/" not in ua:
        browser = "app op beginscherm"  # the home-screen web app reports no Safari version
    return f"{device} · {browser}" if browser else device


def session_id(token_hash: str) -> str:
    """A short public handle for a session (never the token or its full hash)."""
    return token_hash[:16]


def start_session(conn, request: Request, response: Response, user_id: str, data_key: bytes | None = None) -> None:
    token = secrets.token_urlsafe(32)
    sealed = None
    if data_key:
        nonce = secrets.token_bytes(12)
        sealed = _b64(nonce + AESGCM(_session_key(token)).encrypt(nonce, data_key, b"session"))
    conn.execute("INSERT INTO sessions (token_hash, user_id, created_at, expires_at, device, last_seen, data_key) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (_hash_token(token), user_id, db.now_iso(), _expiry(), device_name(request.headers.get("user-agent", "")), db.now_iso(), sealed))
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (db.now_iso(),))
    # The cookie outlives the session on purpose: the server ends a session after 30 days without use.
    response.set_cookie(COOKIE, token, max_age=400 * 86400, httponly=True, samesite="lax",
                        secure=_is_https(request), path="/")


def end_session(request: Request, response: Response) -> None:
    if token := request.cookies.get(COOKIE):
        with db.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(token),))
    response.delete_cookie(COOKIE, path="/")


def current_user(request: Request) -> dict | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    with db.connect() as conn:
        row = conn.execute(
            "SELECT users.*, sessions.expires_at, sessions.data_key FROM sessions JOIN users ON users.id = sessions.user_id WHERE token_hash = ?",
            (_hash_token(token),)).fetchone()
        if not row or row["expires_at"] < db.now_iso():
            return None
        # Sliding expiry: a session in use keeps going; refreshed at most once a day.
        expires = datetime.fromisoformat(row["expires_at"])
        if expires - datetime.now(timezone.utc) < timedelta(days=SESSION_DAYS - 1):
            conn.execute("UPDATE sessions SET expires_at = ?, last_seen = ? WHERE token_hash = ?", (_expiry(), db.now_iso(), _hash_token(token)))
    user = SessionUser(public_user(row))
    if row["data_key"]:
        try:
            raw = base64.b64decode(row["data_key"])
            user.data_key = AESGCM(_session_key(token)).decrypt(raw[:12], raw[12:], b"session")
        except (InvalidTag, ValueError):
            pass
    return user


def current_token_hash(request: Request) -> str | None:
    token = request.cookies.get(COOKIE)
    return _hash_token(token) if token else None


def hash_secret(secret: str) -> str:
    return _hash_token(secret)


# MARK: - Dependencies

def require_user(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise HTTPException(401, "Log in om verder te gaan.")
    return user


def require_admin(request: Request) -> dict:
    user = require_user(request)
    if not user["is_admin"]:
        raise HTTPException(403, "Enkel de beheerder kan dit.")
    return user
