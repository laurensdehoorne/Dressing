"""The AI services an account can use, and its own API key for each, opened from the account's vault (see auth.py)."""

import os

from . import auth

PROVIDERS = {
    "claude": {"name": "Claude", "company": "Anthropic", "secret": "api_key_enc", "env": "ANTHROPIC_API_KEY",
               "model_setting": "model", "prefix": "sk-ant-", "console": "console.anthropic.com"},
    "openai": {"name": "ChatGPT", "company": "OpenAI", "secret": "openai_key_enc", "env": "OPENAI_API_KEY",
               "model_setting": "openai_model", "prefix": "sk-", "console": "platform.openai.com"},
}


def provider(user: dict) -> str:
    """The service this account chose under Instellingen (Claude unless it picked another)."""
    chosen = auth.user_settings(user).get("ai_provider")
    return chosen if chosen in PROVIDERS else "claude"


def active_key(user: dict, name: str = "claude") -> tuple[str | None, str | None]:
    """This account's own key for a service, opened with the data key of this sign-in; the administrator may also
    fall back on the key in .env. "locked": there is a key, but this session can't open it (sign in again)."""
    spec, settings = PROVIDERS[name], auth.user_settings(user)
    env_key = os.environ.get(spec["env"], "").strip() if user["is_admin"] else ""
    if blob := settings.get(spec["secret"]):
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
