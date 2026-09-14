"""Přihlášení k Sorare API včetně 2FA.

Sorare používá dvoufázový signIn:

1. e-mail + bcrypt hash hesla → server vrátí ``otpSessionChallenge``
2. ``otpSessionChallenge`` + šestimístný kód z autentikátoru → JWT

JWT platí 30 dní, takže kód zadáváš jednou za měsíc přes /login. Token bydlí
ve Store, ne na disku (na Vercelu by nepřežil).

Volitelně lze nastavit SORARE_TOTP_SECRET a kódy se generují samy. Tím ale
z 2FA děláš 1FA, protože oba faktory leží na stejném místě — je to opt-in
a výchozí stav je vypnutý.
"""
from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass

import bcrypt
import requests

from .models import env
from .store import K_TOKEN, get_store

SALT_ENDPOINT = "https://api.sorare.com/api/v1/users/{email}"
GRAPHQL_URL = "https://api.sorare.com/graphql"
K_OTP_CHALLENGE = "sorare:otp_challenge"

SIGN_IN_MUTATION = """
mutation SignIn($input: signInInput!, $aud: String!) {
  signIn(input: $input) {
    currentUser { slug nickname }
    jwtToken(aud: $aud) { token expiredAt }
    otpSessionChallenge
    errors { message }
  }
}
"""


class AuthError(RuntimeError):
    pass


class OtpRequired(AuthError):
    """Server čeká na kód z autentikátoru.

    Sorare to hlásí dvěma způsoby podle typu 2FA:
      * vrátí ``otpSessionChallenge`` — kód se posílá s tímhle challenge,
      * vrátí chybu ``2fa_missing`` — kód se posílá znovu s e-mailem a heslem.
    Ukládáme, co přišlo, a druhá fáze se podle toho zařídí.
    """

    def __init__(self, challenge: str | None = None):
        super().__init__("Sorare vyžaduje kód z autentikátoru.")
        self.challenge = challenge


@dataclass
class Token:
    access_token: str
    aud: str
    expires_at: float
    user_slug: str | None = None
    nickname: str | None = None

    @property
    def is_expired(self) -> bool:
        # Hodinová rezerva: job běžící přes deadline nesmí padnout na vypršení.
        return time.time() > self.expires_at - 3600

    @property
    def days_left(self) -> float:
        return max(0.0, (self.expires_at - time.time()) / 86400)


# --------------------------------------------------------------------- helpers


def _aud() -> str:
    return env("SORARE_JWT_AUD", "sorare-mlb-lineups")


def _hash_password(email: str, password: str) -> str:
    import urllib.parse

    resp = requests.get(SALT_ENDPOINT.format(email=urllib.parse.quote(email)), timeout=20)
    resp.raise_for_status()
    salt = resp.json().get("salt")
    if not salt:
        raise AuthError("Sorare nevrátil salt — zkontroluj SORARE_EMAIL.")
    return bcrypt.hashpw(password.encode("utf-8"), salt.encode("utf-8")).decode("utf-8")


def _call_sign_in(sign_in_input: dict) -> dict:
    headers = {"content-type": "application/json"}
    api_key = env("SORARE_API_KEY")
    if api_key:
        headers["APIKEY"] = api_key

    resp = requests.post(
        GRAPHQL_URL,
        headers=headers,
        json={
            "operationName": "SignIn",
            "query": SIGN_IN_MUTATION,
            "variables": {"input": sign_in_input, "aud": _aud()},
        },
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()

    if payload.get("errors"):
        raise AuthError(f"GraphQL chyba při signIn: {payload['errors']}")

    data = (payload.get("data") or {}).get("signIn") or {}
    if data.get("errors"):
        messages = "; ".join(e.get("message", "?") for e in data["errors"])
        # Tohle není chyba, jen žádost o druhý faktor. Sorare přitom vrací
        # otpSessionChallenge ve STEJNÉ odpovědi vedle chyby — bez něj druhá
        # fáze skončí na "invalid", takže si ho odsud musíme vzít.
        if "2fa_missing" in messages or "otp" in messages.lower():
            raise OtpRequired(data.get("otpSessionChallenge"))
        raise AuthError(f"signIn selhal: {messages}")
    return data


def _token_from_response(data: dict) -> Token:
    jwt = data.get("jwtToken") or {}
    if not jwt.get("token"):
        raise AuthError("signIn neprošel — nevrátil se token.")

    expires_at = time.time() + 30 * 24 * 3600
    if jwt.get("expiredAt"):
        try:
            from datetime import datetime

            expires_at = datetime.fromisoformat(
                str(jwt["expiredAt"]).replace("Z", "+00:00")
            ).timestamp()
        except ValueError:
            pass

    user = data.get("currentUser") or {}
    return Token(
        access_token=jwt["token"],
        aud=_aud(),
        expires_at=expires_at,
        user_slug=user.get("slug"),
        nickname=user.get("nickname"),
    )


def _persist(token: Token) -> Token:
    # TTL o den kratší než platnost, ať v cache nikdy neleží mrtvý token.
    ttl = max(int(token.expires_at - time.time()) - 86400, 3600)
    get_store().set(K_TOKEN, asdict(token), ttl_seconds=ttl)
    return token


# --------------------------------------------------------------------- login flow


def start_login(email: str | None = None, password: str | None = None) -> Token:
    """První fáze. Buď rovnou vrátí token (účet bez 2FA), nebo vyhodí OtpRequired."""
    email = email or env("SORARE_EMAIL", required=True)
    password = password or env("SORARE_PASSWORD", required=True)

    secret = os.environ.get("SORARE_TOTP_SECRET")

    try:
        data = _call_sign_in(
            {"email": email, "password": _hash_password(email, password)}
        )
    except OtpRequired as exc:
        # Když challenge v odpovědi byl, použijeme ho. Když ne, druhá fáze
        # pošle kód spolu s přihlašovacími údaji.
        get_store().set(
            K_OTP_CHALLENGE, exc.challenge or "__credentials__", ttl_seconds=600
        )
        if secret:
            return complete_login(_generate_totp(secret), exc.challenge)
        raise

    challenge = data.get("otpSessionChallenge")
    if challenge:
        # Challenge má krátkou platnost; 10 minut je víc než dost na opsání kódu.
        get_store().set(K_OTP_CHALLENGE, challenge, ttl_seconds=600)
        if secret:
            return complete_login(_generate_totp(secret), challenge)
        raise OtpRequired(challenge)

    return _persist(_token_from_response(data))


def complete_login(otp_code: str, challenge: str | None = None) -> Token:
    """Druhá fáze — dokončí přihlášení kódem z autentikátoru."""
    challenge = challenge or get_store().get(K_OTP_CHALLENGE)
    if not challenge:
        raise AuthError(
            "Platnost přihlášení vypršela (10 minut). Klikni znovu na tlačítko "
            "Začít přihlášení a zadej čerstvý kód."
        )

    code = "".join(ch for ch in str(otp_code) if ch.isdigit())
    if len(code) != 6:
        raise AuthError("Kód musí mít šest číslic.")

    if challenge == "__credentials__":
        email = env("SORARE_EMAIL", required=True)
        password = env("SORARE_PASSWORD", required=True)
        sign_in_input = {
            "email": email,
            "password": _hash_password(email, password),
            "otpAttempt": code,
        }
    else:
        sign_in_input = {"otpSessionChallenge": challenge, "otpAttempt": code}

    data = _call_sign_in(sign_in_input)
    token = _persist(_token_from_response(data))
    get_store().delete(K_OTP_CHALLENGE)
    return token


def _generate_totp(secret: str) -> str:
    try:
        import pyotp
    except ImportError as exc:  # pragma: no cover
        raise AuthError("SORARE_TOTP_SECRET je nastaven, ale chybí balíček pyotp.") from exc
    return pyotp.TOTP(secret.replace(" ", "")).now()


# --------------------------------------------------------------------- access


def current_token() -> Token | None:
    """Vrátí uložený token, pokud je platný. Nikdy se nepřihlašuje sám."""
    cached = get_store().get_json(K_TOKEN)
    if not cached:
        return None
    try:
        token = Token(**cached)
    except TypeError:
        return None
    return None if token.is_expired else token


def ensure_token() -> Token:
    """Token pro běh pipeline.

    S 2FA se nedá přihlásit bez lidského zásahu, takže když token chybí,
    skončíme srozumitelnou chybou místo nekonečného opakování. Výjimka je
    nastavený TOTP secret — tehdy zvládneme obnovu samy.
    """
    token = current_token()
    if token:
        return token

    if os.environ.get("SORARE_TOTP_SECRET"):
        return start_login()

    try:
        return start_login()
    except OtpRequired as exc:
        raise AuthError(
            "Chybí platný Sorare token a účet má 2FA. Přihlas se na /login — "
            "kód z autentikátoru stačí zadat jednou za 30 dní."
        ) from exc


def token_status() -> dict:
    token = current_token()
    if not token:
        return {"authenticated": False, "days_left": 0, "needs_login": True}
    return {
        "authenticated": True,
        "nickname": token.nickname,
        "user_slug": token.user_slug,
        "days_left": round(token.days_left, 1),
        # Týden dopředu je dost času, aby tě to nezaskočilo před deadlinem.
        "needs_login": token.days_left < 7,
    }
