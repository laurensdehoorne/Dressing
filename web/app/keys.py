"""The account's own Anthropic API key, opened from the account's vault (see auth.py)."""

import os

from . import auth


def active_key(user: dict) -> tuple[str | None, str | None]:
    """This account's own key, opened with the data key of this sign-in; the administrator may also fall back on the
    key in .env. "locked": there is a key, but this session can't open it (sign in again)."""
    settings = auth.user_settings(user)
    env_key = os.environ.get("ANTHROPIC_API_KEY", "").strip() if user["is_admin"] else ""
    if blob := settings.get("api_key_enc"):
        data_key = getattr(user, "data_key", None)
        if data_key and (key := auth.decrypt_secret(data_key, blob)):
            return key, "app"
        if not env_key:
            return None, "locked"
    if env_key:
        return env_key, "env"
    return None, None


def hint(key: str | None) -> str:
    """"sk-ant-…a1b2": enough to recognise a key, never the key itself."""
    return f"{key[:7]}…{key[-4:]}" if key and len(key) > 12 else ""
