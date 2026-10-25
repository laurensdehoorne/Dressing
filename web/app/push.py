"""Web Push: "Hoe viel je outfit?" in the evening, for an outfit worn today that has no score yet, on the phone (the web
app on the home screen, iOS 16.4 or newer). Every score tells the stylist what you like.

The server signs its messages with its own VAPID key pair, made once and kept in data/vapid.pem. A phone that turns on
notifications sends its subscription (an endpoint at Apple's or another push service, plus encryption keys); every
message is encrypted for that phone and posted there. (As in CellarPeaks' push.py.)
"""

import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import socket
from datetime import datetime, time, timedelta, timezone
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid02
from pywebpush import WebPushException, webpush

from . import db

# Who push services can contact about these messages: "mailto:you@example.org" or an https:// domain without a path.
# Apple refuses anything else (and a localhost address).
CONTACT = os.environ.get("PUSH_CONTACT", "").strip() or "https://github.com"
ASK_AT = time(20, 0)  # in the evening the day is done: then you know how the outfit was
ASK_AFTER = timedelta(minutes=30)  # an outfit noted later in the evening: asked half an hour after
ASK_STALE = timedelta(hours=3)  # missed by a long restart: too late to still ask

_vapid: Vapid02 | None = None
PUSH_HOSTS = ("fcm.googleapis.com", "push.services.mozilla.com", "push.apple.com",
              "notify.windows.com", "wns.windows.com")


def _valid_endpoint(endpoint: str) -> bool:
    """Only accept HTTPS endpoints on browser push-provider domains with public DNS answers."""
    if not endpoint or len(endpoint) > 8192:
        return False
    try:
        parts = urlsplit(endpoint)
        host = (parts.hostname or "").rstrip(".").lower()
        if (parts.scheme != "https" or not host or parts.username or parts.password or parts.port not in (None, 443)
                or parts.fragment or not any(host == allowed or host.endswith("." + allowed) for allowed in PUSH_HOSTS)):
            return False
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        return bool(addresses) and all(ipaddress.ip_address(row[4][0]).is_global for row in addresses)
    except (OSError, UnicodeError, ValueError):
        return False


def vapid() -> Vapid02:
    global _vapid
    if _vapid is None:
        path = db.DATA_DIR / "vapid.pem"
        if not path.exists():
            key = Vapid02()
            key.generate_keys()
            key.save_key(str(path))
            os.chmod(path, 0o600)
        _vapid = Vapid02.from_file(str(path))
    return _vapid


def public_key() -> str:
    """The key the phone needs to subscribe (applicationServerKey), base64url without padding."""
    raw = vapid().public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


# MARK: - Subscriptions

def device_id(endpoint: str) -> str:
    """A short public handle for a phone's subscription (the endpoint itself is a secret address)."""
    return hashlib.sha256(endpoint.encode()).hexdigest()[:16]


def save(user: dict, subscription: dict, device: str = "") -> None:
    endpoint = str(subscription.get("endpoint") or "")
    keys = subscription.get("keys") or {}
    if not _valid_endpoint(endpoint) or not isinstance(keys, dict) or not keys.get("p256dh") or not keys.get("auth"):
        raise ValueError("Ongeldig abonnement op meldingen.")
    data = {"endpoint": endpoint, "keys": {"p256dh": str(keys["p256dh"]), "auth": str(keys["auth"])}}
    with db.connect() as conn:
        # The same phone signing in with another account moves its subscription to that account.
        conn.execute("INSERT OR REPLACE INTO push_subscriptions (endpoint, user_id, data, created_at, device) VALUES (?, ?, ?, ?, ?)",
                     (endpoint, user["id"], json.dumps(data), db.now_iso(), device))


def remove(user: dict, endpoint: str) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ? AND user_id = ?", (endpoint, user["id"]))


def devices(user: dict) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute("SELECT endpoint, device, created_at FROM push_subscriptions WHERE user_id = ? ORDER BY created_at DESC",
                            (user["id"],)).fetchall()
    return [{"id": device_id(r["endpoint"]), "device": r["device"] or "Onbekend toestel", "created_at": r["created_at"]} for r in rows]


def forget(user: dict, did: str) -> bool:
    with db.connect() as conn:
        for row in conn.execute("SELECT endpoint FROM push_subscriptions WHERE user_id = ?", (user["id"],)).fetchall():
            if device_id(row["endpoint"]) == did:
                conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (row["endpoint"],))
                return True
    return False


# MARK: - Sending

def _send_one(subscription: dict, message: dict) -> bool:
    """Sends to one phone. False when the push service says it no longer exists (the app was removed)."""
    try:
        webpush(subscription, json.dumps(message), vapid_private_key=vapid(), vapid_claims={"sub": CONTACT}, ttl=12 * 3600,
                timeout=20)
        return True
    except WebPushException as e:
        status = getattr(e.response, "status_code", None)
        if status in (404, 410):
            return False
        print(f"push failed ({status}): {e}")
        return True


async def send(user_id: str, message: dict) -> int:
    """Sends a message to every phone of this account; returns how many got it. Gone phones are forgotten."""
    with db.connect() as conn:
        rows = conn.execute("SELECT endpoint, data FROM push_subscriptions WHERE user_id = ?", (user_id,)).fetchall()
    sent = 0
    for row in rows:
        if await asyncio.to_thread(_send_one, json.loads(row["data"]), message):
            sent += 1
        else:
            with db.connect() as conn:
                conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (row["endpoint"],))
    return sent


# MARK: - "Hoe viel je outfit?"

def ask_when(day: str, now: datetime | None = None) -> str | None:
    """When to ask about an outfit worn on `day`: this evening at 20:00, or half an hour from now when that's later.
    An outfit noted for an earlier day isn't asked about. In UTC, as the database keeps its times."""
    now = (now or datetime.now(timezone.utc)).astimezone()  # the server's time zone: Europe/Brussels (Dockerfile)
    if day != now.date().isoformat():
        return None
    evening = datetime.combine(now.date(), ASK_AT, tzinfo=now.tzinfo)
    return max(evening, now + ASK_AFTER).astimezone(timezone.utc).isoformat(timespec="seconds")


def ask_later(conn, user_id: str, day: str, data: dict) -> None:
    """Puts the question on an outfit without a score, if this account has a phone with notifications on."""
    if data.get("rating") or not conn.execute("SELECT 1 FROM push_subscriptions WHERE user_id = ?", (user_id,)).fetchone():
        return
    if when := ask_when(day):
        data["ask_at"] = when


def question(conn, log_id: str, data: dict) -> dict:
    names = []
    for item_id in data.get("item_ids", [])[:3]:
        row = conn.execute("SELECT data FROM items WHERE id = ?", (item_id,)).fetchone()
        if row and (name := json.loads(row["data"]).get("name")):
            names.append(name)
    return {"title": "👕 Hoe viel je outfit?", "body": f"{' + '.join(names) or 'Je outfit van vandaag'}: geef een score, dan kiest "
            "de stylist voortaan beter.", "url": f"/#gedragen={log_id}", "tag": f"dressing-rate-{log_id}"}


async def send_questions(now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    with db.connect() as conn:
        rows = conn.execute("SELECT id, user_id, data FROM worn WHERE json_extract(data, '$.ask_at') <= ?",
                            (now.isoformat(timespec="seconds"),)).fetchall()
        due = []
        for row in rows:
            data = json.loads(row["data"])
            when = datetime.fromisoformat(data.pop("ask_at"))
            conn.execute("UPDATE worn SET data = ? WHERE id = ?", (json.dumps(data), row["id"]))  # first, so never twice
            if now - when < ASK_STALE and not data.get("rating"):
                due.append((row["user_id"], question(conn, row["id"], data)))
    for user_id, message in due:
        await send(user_id, message)


async def question_loop() -> None:
    while True:
        try:
            await send_questions()
        except Exception as e:  # never let notifications take the server down
            print(f"outfit questions failed: {e!r}")
        await asyncio.sleep(60)
