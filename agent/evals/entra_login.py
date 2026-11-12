"""Headless login of the eval test user (Entra External ID native authentication).

The web app signs in with Google, which can't be scripted. Evals use a second app
registration (ENTRA_EVAL_CLIENT_ID) whose user flow allows email + password, and one fixed
test user (EVAL_USER_EMAIL / EVAL_USER_PASSWORD). The token route accepts both apps.

  initiate (username) ──► challenge (password) ──► token (password) ──► ID token
"""

import base64
import json
import os
from uuid import UUID

import aiohttp

CHALLENGE_TYPES = "password redirect"  # "redirect": we can't do anything else headlessly
SCOPES = "openid profile offline_access"
GRANT_PASSWORD = "password"


def _endpoint(step: str) -> str:
    sub = os.environ["ENTRA_TENANT_SUBDOMAIN"]
    return f"https://{sub}.ciamlogin.com/{sub}.onmicrosoft.com/oauth2/v2.0/{step}"


async def _post(http: aiohttp.ClientSession, step: str, form: dict[str, str]) -> dict:
    form = {"client_id": os.environ["ENTRA_EVAL_CLIENT_ID"], **form}
    async with http.post(_endpoint(step), data=form) as r:
        body = await r.json()
    if "error" in body:
        raise RuntimeError(f"native auth {step}: {body.get('error_description', body)}")
    return body


def _claims(jwt: str) -> dict:
    """Payload of a token we just got from Entra over TLS (the token route verifies it)."""
    payload = jwt.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


async def eval_login(http: aiohttp.ClientSession) -> tuple[UUID, str]:
    """Signs in the eval user; returns (learner id = Entra object id, ID token)."""
    started = await _post(
        http,
        "initiate",
        {"username": os.environ["EVAL_USER_EMAIL"], "challenge_type": CHALLENGE_TYPES},
    )
    challenged = await _post(
        http,
        "challenge",
        {"continuation_token": started["continuation_token"], "challenge_type": CHALLENGE_TYPES},
    )
    if challenged.get("challenge_type") != GRANT_PASSWORD:
        raise RuntimeError(f"native auth: unexpected challenge {challenged}")

    tokens = await _post(
        http,
        "token",
        {
            "continuation_token": challenged["continuation_token"],
            "grant_type": GRANT_PASSWORD,
            "password": os.environ["EVAL_USER_PASSWORD"],
            "scope": SCOPES,
        },
    )
    id_token = tokens["id_token"]
    return UUID(_claims(id_token)["oid"]), id_token
