"""Credentials live in the OS keychain (Windows Credential Manager), never in the app folder.

If no keychain is available (rare; e.g. some Linux servers), falls back to data/secrets.json
and the Settings page shows a warning.
"""

from __future__ import annotations

import json

from . import paths

SERVICE = "OracleEdge"
_FALLBACK = paths.DATA_DIR / "secrets.json"


def _keyring():
    try:
        import keyring
        from keyring.backends import fail

        if isinstance(keyring.get_keyring(), fail.Keyring):
            return None
        return keyring
    except Exception:
        return None


def backend_name() -> str:
    kr = _keyring()
    return type(kr.get_keyring()).__name__ if kr else "file fallback (data/secrets.json)"


def uses_fallback() -> bool:
    return _keyring() is None


def _load_fallback() -> dict:
    if _FALLBACK.exists():
        return json.loads(_FALLBACK.read_text(encoding="utf-8"))
    return {}


def get(name: str) -> str | None:
    kr = _keyring()
    if kr:
        try:
            return kr.get_password(SERVICE, name)
        except Exception:
            pass
    return _load_fallback().get(name)


def set(name: str, value: str | None) -> None:  # noqa: A001 - mirrors keyring API
    kr = _keyring()
    if kr:
        try:
            if value:
                kr.set_password(SERVICE, name, value)
            else:
                try:
                    kr.delete_password(SERVICE, name)
                except Exception:
                    pass
            return
        except Exception:
            pass
    data = _load_fallback()
    if value:
        data[name] = value
    else:
        data.pop(name, None)
    paths.ensure_dirs()
    _FALLBACK.write_text(json.dumps(data), encoding="utf-8")
