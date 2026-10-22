"""HTTP Basic auth, with a small brute-force brake.

One shared credential, by design: this is a two-person household tool, not a
multi-tenant app. The password is never stored in the process, only a bcrypt
hash from the environment.

Generate one with:

    python -m app.web.auth hash
"""

from __future__ import annotations

import os
import secrets
import sys
import time
from collections import deque

import bcrypt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 600

_basic = HTTPBasic(auto_error=False)
_failures: dict[str, deque[float]] = {}


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


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


def _record_failure(key: str) -> None:
    _failures.setdefault(key, deque()).append(time.monotonic())


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
    user = os.environ.get("DASHBOARD_USER", "")
    password_hash = os.environ.get("DASHBOARD_PASSWORD_HASH", "")
    if not user or not password_hash:
        raise SystemExit(
            "DASHBOARD_USER and DASHBOARD_PASSWORD_HASH must be set.\n"
            "Generate a hash with: python -m app.web.auth hash"
        )
    return user, password_hash


def _main(argv: list[str]) -> int:
    if not argv or argv[0] != "hash":
        print("usage: python -m app.web.auth hash [password]", file=sys.stderr)
        return 2
    password = argv[1] if len(argv) > 1 else input("password: ")
    print(hash_password(password))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
