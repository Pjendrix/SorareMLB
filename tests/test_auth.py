"""Testy dvoufázového přihlášení s 2FA — bez sítě, Sorare je nahrazené fake."""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sorare_mlb import auth  # noqa: E402
from sorare_mlb.store import K_TOKEN, LocalStore  # noqa: E402

CHALLENGE = "challenge-abc123"
GOOD_CODE = "123456"


@pytest.fixture
def store(monkeypatch, tmp_path):
    import sorare_mlb.store as store_mod

    monkeypatch.setattr(store_mod, "LOCAL_FILE", tmp_path / "store.json")
    fake = LocalStore()
    monkeypatch.setattr(store_mod, "_store", fake)
    monkeypatch.setattr(store_mod, "get_store", lambda: fake)
    monkeypatch.setattr(auth, "get_store", lambda: fake)
    return fake


@pytest.fixture
def sorare(monkeypatch, store):
    """Fake Sorare: heslo vrátí challenge, správný OTP vrátí token."""
    monkeypatch.setenv("SORARE_EMAIL", "me@example.com")
    monkeypatch.setenv("SORARE_PASSWORD", "hunter2")
    monkeypatch.setenv("SORARE_JWT_AUD", "test-aud")
    monkeypatch.delenv("SORARE_TOTP_SECRET", raising=False)
    monkeypatch.setattr(auth, "_hash_password", lambda e, p: "hashed")

    calls = []

    def fake_sign_in(payload):
        calls.append(payload)
        if "otpAttempt" in payload:
            if payload["otpSessionChallenge"] != CHALLENGE:
                raise auth.AuthError("signIn selhal: neplatný challenge")
            if payload["otpAttempt"] != GOOD_CODE:
                raise auth.AuthError("signIn selhal: Invalid OTP")
            return {
                "currentUser": {"slug": "me", "nickname": "Me"},
                "jwtToken": {"token": "jwt-xyz", "expiredAt": None},
            }
        return {"otpSessionChallenge": CHALLENGE}

    monkeypatch.setattr(auth, "_call_sign_in", fake_sign_in)
    return calls


def test_password_step_requests_otp(sorare, store):
    with pytest.raises(auth.OtpRequired) as exc:
        auth.start_login()
    assert exc.value.challenge == CHALLENGE
    assert store.get(auth.K_OTP_CHALLENGE) == CHALLENGE
    assert auth.current_token() is None


def test_otp_completes_login_and_persists_token(sorare, store):
    with pytest.raises(auth.OtpRequired):
        auth.start_login()

    token = auth.complete_login(GOOD_CODE)
    assert token.access_token == "jwt-xyz"
    assert token.nickname == "Me"
    assert token.days_left > 28  # výchozích 30 dní

    # Token přežije "restart funkce" — načte se ze Store.
    assert auth.current_token().access_token == "jwt-xyz"
    # Challenge se po použití zahodí.
    assert store.get(auth.K_OTP_CHALLENGE) is None


def test_wrong_code_is_rejected(sorare, store):
    with pytest.raises(auth.OtpRequired):
        auth.start_login()
    with pytest.raises(auth.AuthError, match="Invalid OTP"):
        auth.complete_login("000000")
    assert auth.current_token() is None


def test_malformed_code_rejected_before_network(sorare, store):
    with pytest.raises(auth.OtpRequired):
        auth.start_login()
    before = len(sorare)
    with pytest.raises(auth.AuthError, match="šest číslic"):
        auth.complete_login("12")
    assert len(sorare) == before, "krátký kód se neměl posílat na server"


def test_otp_accepts_spaced_input(sorare, store):
    with pytest.raises(auth.OtpRequired):
        auth.start_login()
    token = auth.complete_login("123 456")
    assert token.access_token == "jwt-xyz"


def test_expired_challenge_gives_clear_error(sorare, store):
    with pytest.raises(auth.AuthError, match="Začít přihlášení"):
        auth.complete_login(GOOD_CODE)


def test_ensure_token_does_not_loop_on_2fa(sorare, store):
    """Pipeline se s 2FA nesmí pokoušet přihlásit sama donekonečna."""
    with pytest.raises(auth.AuthError, match="/login"):
        auth.ensure_token()


def test_ensure_token_returns_cached(sorare, store):
    with pytest.raises(auth.OtpRequired):
        auth.start_login()
    auth.complete_login(GOOD_CODE)
    assert auth.ensure_token().access_token == "jwt-xyz"


def test_expired_token_is_not_used(sorare, store):
    store.set(
        K_TOKEN,
        {
            "access_token": "old", "aud": "test-aud",
            "expires_at": time.time() - 10, "user_slug": "me", "nickname": "Me",
        },
    )
    assert auth.current_token() is None
    assert auth.token_status()["needs_login"] is True


def test_status_flags_upcoming_expiry(sorare, store):
    store.set(
        K_TOKEN,
        {
            "access_token": "soon", "aud": "test-aud",
            "expires_at": time.time() + 3 * 86400, "user_slug": "me", "nickname": "Me",
        },
    )
    status = auth.token_status()
    assert status["authenticated"] is True
    assert status["needs_login"] is True, "token pod týden má hlásit obnovu"


def test_totp_secret_skips_manual_step(sorare, store, monkeypatch):
    monkeypatch.setenv("SORARE_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
    monkeypatch.setattr(auth, "_generate_totp", lambda secret: GOOD_CODE)
    token = auth.start_login()
    assert token.access_token == "jwt-xyz"
