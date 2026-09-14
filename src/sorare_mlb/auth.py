"""Přihlášení k Sorare API.

Dvě podporované cesty:

1. ``password`` — GET /api/v1/users/<email> vrátí bcrypt salt, heslo se hashuje
   lokálně a na server jde jen hash. Následný ``signIn`` vrátí JWT.
2. ``oauth``   — standardní authorization-code flow "Login with Sorare"
   s dočasným HTTP serverem na localhostu pro callback.

Token se ukládá do .tokens.json. Platnost JWT je 30 dní.

Klíčový detail, na kterém se to nejčastěji zasekne: hodnota ``aud`` použitá
při signIn se MUSÍ posílat v hlavičce ``JWT-AUD`` u každého dalšího requestu.
"""
from __future__ import annotations

import json
import secrets
import time
import urllib.parse
import webbrowser
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import bcrypt
import requests

from .models import env

TOKEN_FILE = Path(".tokens.json")
SALT_ENDPOINT = "https://api.sorare.com/api/v1/users/{email}"
GRAPHQL_URL = "https://api.sorare.com/graphql"
OAUTH_AUTHORIZE = "https://sorare.com/oauth/authorize"
OAUTH_TOKEN = "https://api.sorare.com/oauth/token"

SIGN_IN_MUTATION = """
mutation SignIn($input: signInInput!, $aud: String!) {
  signIn(input: $input) {
    currentUser {
      slug
      nickname
    }
    jwtToken(aud: $aud) {
      token
      expiredAt
    }
    otpSessionChallenge
    errors { message }
  }
}
"""


@dataclass
class Token:
    access_token: str
    aud: str
    expires_at: float
    user_slug: str | None = None
    method: str = "password"

    @property
    def is_expired(self) -> bool:
        # Minutová rezerva, ať nepadneme uprostřed submitu.
        return time.time() > self.expires_at - 60


# --------------------------------------------------------------------------- storage


def save_token(token: Token) -> None:
    TOKEN_FILE.write_text(json.dumps(asdict(token), indent=2), encoding="utf-8")
    TOKEN_FILE.chmod(0o600)


def load_token() -> Token | None:
    if not TOKEN_FILE.exists():
        return None
    try:
        return Token(**json.loads(TOKEN_FILE.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, TypeError):
        return None


# --------------------------------------------------------------------------- password


def _fetch_salt(email: str) -> str:
    resp = requests.get(SALT_ENDPOINT.format(email=urllib.parse.quote(email)), timeout=20)
    resp.raise_for_status()
    salt = resp.json().get("salt")
    if not salt:
        raise RuntimeError("Sorare nevrátil salt — zkontroluj e-mail.")
    return salt


def login_with_password(email: str, password: str, aud: str, api_key: str | None = None) -> Token:
    salt = _fetch_salt(email)
    hashed = bcrypt.hashpw(password.encode("utf-8"), salt.encode("utf-8")).decode("utf-8")

    headers = {"content-type": "application/json"}
    if api_key:
        headers["APIKEY"] = api_key

    resp = requests.post(
        GRAPHQL_URL,
        headers=headers,
        json={
            "operationName": "SignIn",
            "query": SIGN_IN_MUTATION,
            "variables": {"input": {"email": email, "password": hashed}, "aud": aud},
        },
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()

    if payload.get("errors"):
        raise RuntimeError(f"GraphQL chyba při signIn: {payload['errors']}")

    data = (payload.get("data") or {}).get("signIn") or {}
    if data.get("errors"):
        raise RuntimeError(f"signIn selhal: {data['errors']}")

    if data.get("otpSessionChallenge"):
        raise RuntimeError(
            "Účet má zapnuté 2FA. Tenhle skript OTP flow neimplementuje — "
            "použij místo toho OAuth: `login --method oauth`."
        )

    jwt = data.get("jwtToken") or {}
    if not jwt.get("token"):
        raise RuntimeError("signIn neprošel — nevrátil se token.")

    # expiredAt bývá ISO string; pokud ho neumíme naparsovat, počítáme 30 dní.
    expires_at = time.time() + 30 * 24 * 3600
    raw = jwt.get("expiredAt")
    if raw:
        try:
            from datetime import datetime

            expires_at = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass

    return Token(
        access_token=jwt["token"],
        aud=aud,
        expires_at=expires_at,
        user_slug=(data.get("currentUser") or {}).get("slug"),
        method="password",
    )


# --------------------------------------------------------------------------- oauth


class _CallbackHandler(BaseHTTPRequestHandler):
    code: str | None = None
    state: str | None = None

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = urllib.parse.parse_qs(parsed.query)
        _CallbackHandler.code = (params.get("code") or [None])[0]
        _CallbackHandler.state = (params.get("state") or [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            "<html><body style='font-family:sans-serif;padding:3rem'>"
            "<h2>Hotovo</h2><p>Můžeš zavřít tuhle záložku a vrátit se do terminálu.</p>"
            "</body></html>".encode("utf-8")
        )

    def log_message(self, *args):  # potlačí logování do stderr
        return


def login_with_oauth(client_id: str, client_secret: str, aud: str, port: int = 8731) -> Token:
    redirect_uri = f"http://localhost:{port}/callback"
    state = secrets.token_urlsafe(24)

    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "read write",
            "state": state,
        }
    )
    url = f"{OAUTH_AUTHORIZE}?{query}"
    print("Otevírám prohlížeč. Pokud se neotevře, jdi ručně na:\n  " + url)
    webbrowser.open(url)

    server = HTTPServer(("localhost", port), _CallbackHandler)
    server.timeout = 300
    while _CallbackHandler.code is None:
        server.handle_request()
    server.server_close()

    if _CallbackHandler.state != state:
        raise RuntimeError("State se neshoduje — přeruším kvůli riziku CSRF.")

    resp = requests.post(
        OAUTH_TOKEN,
        data={
            "grant_type": "authorization_code",
            "code": _CallbackHandler.code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        },
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()

    if "access_token" not in payload:
        raise RuntimeError(f"OAuth token endpoint nevrátil access_token: {payload}")

    return Token(
        access_token=payload["access_token"],
        aud=aud,
        expires_at=time.time() + float(payload.get("expires_in", 30 * 24 * 3600)),
        method="oauth",
    )


# --------------------------------------------------------------------------- entry


def ensure_token(method: str = "password", force: bool = False) -> Token:
    """Vrátí platný token — z cache, nebo provede přihlášení."""
    if not force:
        cached = load_token()
        if cached and not cached.is_expired:
            return cached

    aud = env("SORARE_JWT_AUD", "sorare-mlb-lineups")
    api_key = env("SORARE_API_KEY")

    if method == "oauth":
        token = login_with_oauth(
            client_id=env("SORARE_OAUTH_CLIENT_ID", required=True),
            client_secret=env("SORARE_OAUTH_CLIENT_SECRET", required=True),
            aud=aud,
            port=int(env("SORARE_OAUTH_REDIRECT_PORT", "8731")),
        )
    else:
        token = login_with_password(
            email=env("SORARE_EMAIL", required=True),
            password=env("SORARE_PASSWORD", required=True),
            aud=aud,
            api_key=api_key,
        )

    save_token(token)
    return token
