"""Password hashing, startup validation and the brute-force brake."""

from __future__ import annotations

import io
import sys

import bcrypt
import pytest

from app.web import auth

PASSWORD = "correct horse battery staple"
PASSWORD_HASH = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()


# --- hash validation -------------------------------------------------------


def test_a_complete_bcrypt_hash_is_accepted() -> None:
    assert auth.validate_password_hash(PASSWORD_HASH) == PASSWORD_HASH


def test_surrounding_quotes_are_tolerated() -> None:
    assert auth.validate_password_hash(f"'{PASSWORD_HASH}'") == PASSWORD_HASH
    assert auth.validate_password_hash(f'"{PASSWORD_HASH}"') == PASSWORD_HASH


def test_a_truncated_hash_is_rejected_with_the_fix() -> None:
    with pytest.raises(ValueError) as error:
        auth.validate_password_hash("$2b$12$tooshort")

    message = str(error.value)
    assert "not a complete bcrypt hash" in message
    assert "single quotes" in message


def test_a_plaintext_password_is_not_a_hash() -> None:
    with pytest.raises(ValueError):
        auth.validate_password_hash(PASSWORD)


def test_settings_requires_both_values(monkeypatch) -> None:
    monkeypatch.delenv("DASHBOARD_USER", raising=False)
    monkeypatch.delenv("DASHBOARD_PASSWORD_HASH", raising=False)
    with pytest.raises(SystemExit):
        auth.settings_from_env()


def test_settings_explains_a_malformed_hash_instead_of_a_401(monkeypatch) -> None:
    monkeypatch.setenv("DASHBOARD_USER", "admin")
    monkeypatch.setenv("DASHBOARD_PASSWORD_HASH", "$2b$12$broken")

    with pytest.raises(SystemExit) as error:
        auth.settings_from_env()

    assert "single quotes" in str(error.value)


def test_settings_returns_the_hash_without_quotes(monkeypatch) -> None:
    monkeypatch.setenv("DASHBOARD_USER", "  admin  ")
    monkeypatch.setenv("DASHBOARD_PASSWORD_HASH", f"'{PASSWORD_HASH}'")

    user, password_hash = auth.settings_from_env()

    assert user == "admin"
    assert password_hash == PASSWORD_HASH


# --- the hash subcommand ---------------------------------------------------


def test_a_piped_password_is_hashed(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("hunter2\n"))

    assert auth._main(["hash"]) == 0

    printed = capsys.readouterr().out.strip()
    assert bcrypt.checkpw(b"hunter2", printed.encode())


def test_a_terminal_password_uses_getpass(monkeypatch, capsys) -> None:
    class TTY(io.StringIO):
        def isatty(self) -> bool:
            return True

    prompts: list[str] = []

    def fake_getpass(prompt: str) -> str:
        prompts.append(prompt)
        return "hunter2"

    monkeypatch.setattr(sys, "stdin", TTY(""))
    monkeypatch.setattr(auth.getpass, "getpass", fake_getpass)

    assert auth._main(["hash"]) == 0
    assert prompts == ["Password: "]
    assert bcrypt.checkpw(b"hunter2", capsys.readouterr().out.strip().encode())


def test_the_password_is_never_taken_as_an_argument(monkeypatch, capsys) -> None:
    # `python -m app.web.auth hash secret` used to work and put the password in
    # shell history. It must now be refused.
    assert auth._main(["hash", "secret"]) == 2
    assert "never taken as an argument" in capsys.readouterr().err


def test_an_empty_password_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n"))
    assert auth._main(["hash"]) == 1


# --- the brute-force brake -------------------------------------------------


def test_the_failure_map_is_capped(monkeypatch) -> None:
    auth._failures.clear()
    monkeypatch.setattr(auth, "MAX_TRACKED_CLIENTS", 3)

    for index in range(5):
        auth._record_failure(f"10.0.0.{index}")

    assert len(auth._failures) == 3


def test_stale_failure_entries_are_dropped(monkeypatch) -> None:
    auth._failures.clear()
    clock = {"now": 1000.0}
    monkeypatch.setattr(auth.time, "monotonic", lambda: clock["now"])

    auth._record_failure("10.0.0.1")
    clock["now"] += auth.FAILURE_WINDOW_SECONDS + 1
    auth._record_failure("10.0.0.2")

    assert list(auth._failures) == ["10.0.0.2"]
