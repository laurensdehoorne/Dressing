"""Who signed in, or tried to, and from where: a log of account events (kept 90 days) and the brake on wrong
passwords.

The app is reached through `tailscale serve` (the tailnet) or `tailscale funnel` (the internet). Tailscale sets
X-Forwarded-For to the visitor's own address, and marks Funnel traffic with "Tailscale-Funnel-Request: ?1". The
server itself only listens on 127.0.0.1 of its machine (compose.yaml), so every request comes through Tailscale.
"""

import ipaddress
import time
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request

from . import auth, db

KEEP_DAYS = 90
WINDOW = 15 * 60  # wrong passwords count this long (seconds)
MAX_FAILURES = 5  # for one username from one address
IP_MAX_FAILURES = 20  # from one address, any usernames
SPREAD_MAX_FAILURES = 20  # for one username from many addresses (the internet and the tailnet apart)

EVENTS = {
    "login": "Aangemeld",
    "login_failed": "Fout wachtwoord",
    "login_blocked": "Geblokkeerd na te veel pogingen",
    "logout": "Afgemeld",
    "setup": "Beheerder aangemaakt",
    "account_created": "Account aangemaakt",
    "password_changed": "Wachtwoord gewijzigd",
    "password_reset": "Wachtwoord gereset door beheerder",
    "account_deleted": "Account verwijderd",
}

# Addresses Tailscale gives the devices in a tailnet (IPv4 and IPv6), and those of this machine and its own network
# (Docker): written out, because Python's is_private also counts ranges that are reachable from the internet.
NOT_INTERNET = tuple(ipaddress.ip_network(n) for n in ("100.64.0.0/10", "fd7a:115c:a1e0::/48", "127.0.0.0/8", "::1/128",
                                                       "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"))


def client_ip(request: Request) -> str:
    """The last address in X-Forwarded-For: the one Tailscale itself saw."""
    return request.headers.get("x-forwarded-for", "").split(",")[-1].strip() or (request.client.host if request.client else "")


def via(request: Request) -> str:
    """"funnel" for the internet, "tailnet" for a device in the tailnet (or this machine itself)."""
    if request.headers.get("tailscale-funnel-request") == "?1":
        return "funnel"
    try:
        ip = ipaddress.ip_address(client_ip(request))
    except ValueError:
        return "tailnet"
    return "tailnet" if any(ip in net for net in NOT_INTERNET) else "funnel"


def record(request: Request, event: str, username: str = "", user_id: str | None = None, conn=None) -> None:
    row = (db.new_id(), db.now_iso(), event, (username or "").strip()[:60], user_id, client_ip(request), via(request),
           auth.device_name(request.headers.get("user-agent", "")))
    sql = "INSERT INTO audit_log (id, at, event, username, user_id, ip, via, device) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
    if conn is not None:
        conn.execute(sql, row)
    else:
        with db.connect() as c:
            c.execute(sql, row)


# MARK: - The brake on wrong passwords (kept in the database, so a restart doesn't reset it)

def _key(username: str) -> str:
    return (username or "").strip().lower()[:60]


def _wait(conn, where: str, args: tuple, limit: int) -> float:
    """Seconds until fewer than `limit` of these failures are left in the window; 0 when that's already so."""
    rows = conn.execute(f"SELECT at FROM login_failures WHERE {where} AND at > ? ORDER BY at DESC LIMIT ?",
                        (*args, time.time() - WINDOW, limit)).fetchall()
    return rows[-1]["at"] + WINDOW - time.time() if len(rows) >= limit else 0


def check_login(request: Request, username: str) -> None:
    key, ip, route = _key(username), client_ip(request), via(request)
    with db.connect() as conn:
        refusal = None
        if _wait(conn, "ip = ?", (ip,), IP_MAX_FAILURES):
            refusal = "Te veel mislukte pogingen vanaf dit adres. Probeer het later opnieuw."
        elif wait := max(_wait(conn, "user_key = ? AND ip = ?", (key, ip), MAX_FAILURES),
                         _wait(conn, "user_key = ? AND via = ?", (key, route), SPREAD_MAX_FAILURES)):
            refusal = f"Te veel mislukte pogingen. Probeer opnieuw over {int(wait) // 60 + 1} minuten."
        if refusal:
            cutoff = (datetime.now(timezone.utc) - timedelta(seconds=WINDOW)).isoformat(timespec="seconds")
            if not conn.execute("SELECT 1 FROM audit_log WHERE event = 'login_blocked' AND ip = ? AND username = ? AND at >= ? LIMIT 1",
                                (ip, (username or "").strip()[:60], cutoff)).fetchone():
                record(request, "login_blocked", username, conn=conn)
    if refusal:
        raise HTTPException(429, refusal)


def login_failed(request: Request, username: str) -> None:
    now = time.time()
    with db.connect() as conn:
        conn.execute("DELETE FROM login_failures WHERE at <= ?", (now - WINDOW,))
        conn.execute("INSERT INTO login_failures (at, user_key, ip, via) VALUES (?, ?, ?, ?)",
                     (now, _key(username), client_ip(request), via(request)))
        record(request, "login_failed", username, conn=conn)


def clear_failures(request: Request, username: str) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM login_failures WHERE user_key = ? AND ip = ?", (_key(username), client_ip(request)))


def entries(user: dict, limit: int = 200) -> list[dict]:
    """The administrator sees everything; anyone else their own events and the attempts on their username."""
    with db.connect() as conn:
        if user["is_admin"]:
            rows = conn.execute("SELECT * FROM audit_log ORDER BY at DESC LIMIT ?", (limit,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM audit_log WHERE user_id = ? OR (user_id IS NULL AND username = ? COLLATE NOCASE) "
                                "ORDER BY at DESC LIMIT ?", (user["id"], user["username"], limit)).fetchall()
    return [{**dict(r), "label": EVENTS.get(r["event"], r["event"])} for r in rows]


def prune() -> None:
    with db.connect() as conn:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds")
        conn.execute("DELETE FROM audit_log WHERE at < ?", (cutoff,))
