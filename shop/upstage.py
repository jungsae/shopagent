"""Read Upstage credentials from the environment without loading key files."""

import os


def get_upstage_api_key(environ=None) -> str | None:
    environ = os.environ if environ is None else environ
    return environ.get("UPSTAGE_API_KEY", "").strip() or None


def require_upstage_api_key(environ=None) -> str:
    key = get_upstage_api_key(environ)
    if key is None:
        raise RuntimeError("Remote Upstage calls are refused because UPSTAGE_API_KEY is missing/empty")
    return key

