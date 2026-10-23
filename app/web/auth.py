"""HTTP Basic auth, with a small brute-force brake.

One shared credential, by design: this is a two-person household tool, not a
multi-tenant app. The password is never stored in the process, only a bcrypt
hash from the environment.

Generate one with:

    python -m app.web.auth hash
"""

from __future__ import annotations

import getpass
import os
import re
import secrets
import sys
import time
from collections import deque

import bcrypt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 600

# A hostile or broken client can invent source addresses faster than any real
# household, so the in-memory brake is capped. Evicting the oldest entry costs
# one attacker an early retry and keeps the process bounded.
MAX_TRACKED_CLIENTS = 1024

# bcrypt hashes are exactly 60 characters: $2a$/$2b$/$2y$, a two-digit cost,
# then 53 characters of salt and digest.
BCRYPT_PATTERN = re.compile(r"^\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}$")

_basic = HTTPBasic(auto_error=False)
_failures: dict[str, deque[float]] = {}


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def validate_password_hash(value: str) -> str:
    """Return a clean bcrypt hash, or explain exactly what is wrong.

    The usual mistake is an unquoted ``$2b$12$...`` in ``.env``: Compose reads
    the dollars as interpolation, truncates the value, and the app starts but
    rejects every password. Failing at startup with the fix in the message is
    far kinder than a 401 that looks like a wrong password.
    """
    cleaned = value.strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in "'\"":
        cleaned = cleaned[1:-1]
    if not BCRYPT_PATTERN.fullmatch(cleaned):
        raise ValueError(
            "DASHBOARD_PASSWORD_HASH is not a complete bcrypt hash (expected 60 "
            "characters starting with $2a$, $2b$ or $2y$).\n"
            "In .env, wrap it in single quotes so Compose does not expand the $:\n"
            "  DASHBOARD_PASSWORD_HASH='$2b$12$...'\n"
            "Generate a new one with: python -m app.web.auth hash"
        )
    return cleaned


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except (ValueError, TypeError):
        return False


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _too_many_failures(key: str) -> bool:
    attempts = _failures.get(key)
    if not attempts:
        return False
    cutoff = time.monotonic() - FAILURE_WINDOW_SECONDS
    while attempts and attempts[0] < cutoff:
        attempts.popleft()
    return len(attempts) >= MAX_FAILURES


def _prune(cutoff: float) -> None:
    stale = [key for key, attempts in _failures.items() if not attempts or attempts[-1] < cutoff]
    for key in stale:
        _failures.pop(key, None)


def _record_failure(key: str) -> None:
    now = time.monotonic()
    _prune(now - FAILURE_WINDOW_SECONDS)
    if key not in _failures and len(_failures) >= MAX_TRACKED_CLIENTS:
        _failures.pop(next(iter(_failures)), None)  # oldest insertion
    _failures.setdefault(key, deque()).append(now)


def make_dependency(user: str, password_hash: str):
    async def require_user(
        request: Request, credentials: HTTPBasicCredentials | None = Depends(_basic)
    ) -> str:
        key = _client_key(request)
        if _too_many_failures(key):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="too many failed attempts; wait a few minutes",
            )
        if credentials is None:
            raise _unauthorized()

        user_ok = secrets.compare_digest(credentials.username, user)
        password_ok = verify_password(credentials.password, password_hash)
        if not (user_ok and password_ok):
            _record_failure(key)
            raise _unauthorized()

        _failures.pop(key, None)
        return credentials.username

    return require_user


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="authentication required",
        headers={"WWW-Authenticate": 'Basic realm="parkhotelchecker"'},
    )


def settings_from_env() -> tuple[str, str]:
    user = os.environ.get("DASHBOARD_USER", "").strip()
    password_hash = os.environ.get("DASHBOARD_PASSWORD_HASH", "")
    if not user or not password_hash:
        raise SystemExit(
            "DASHBOARD_USER and DASHBOARD_PASSWORD_HASH must be set.\n"
            "Generate a hash with: python -m app.web.auth hash"
        )
    try:
        return user, validate_password_hash(password_hash)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None


def _read_password() -> str:
    """Hidden on a terminal, one line from stdin when piped.

    Piped input keeps ``docker compose run -T`` and scripted provisioning
    working; ``getpass`` keeps the password out of the terminal scrollback and
    shell history when a human runs it.
    """
    if sys.stdin.isatty():
        return getpass.getpass("Password: ")
    return sys.stdin.readline().rstrip("\r\n")


def _main(argv: list[str]) -> int:
    if argv != ["hash"]:
        print("usage: python -m app.web.auth hash", file=sys.stderr)
        print(
            "The password is read from the terminal, or from stdin when piped; "
            "it is never taken as an argument (arguments land in shell history).",
            file=sys.stderr,
        )
        return 2
    password = _read_password()
    if not password:
        print("refusing to hash an empty password", file=sys.stderr)
        return 1
    print(hash_password(password))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
