"""Auth tests (UC41)."""

import time

import jwt
import pytest
from fastapi import HTTPException

from backend import auth
from backend.auth import PUBLIC_PATHS, bearer_token, user_id_from_token
from backend.config import Settings, settings

USER = "ff2da522-413b-471e-aef1-8d5c614a52b4"


def mint(secret=None, sub=USER, exp_delta=3600, aud="authenticated", drop_sub=False):
    """Mint a Supabase-shaped access token."""
    now = int(time.time())
    claims = {
        "aud": aud,
        "role": "authenticated",
        "iss": "supabase",
        "iat": now,
        "exp": now + exp_delta,
        "sub": sub,
    }
    if drop_sub:
        claims.pop("sub")
    return jwt.encode(claims, secret or settings.supabase_jwt_secret, algorithm="HS256")


def test_health_is_the_only_public_path():
    """Auth is fail-closed: new routes are protected unless added here."""
    assert PUBLIC_PATHS == frozenset({"/health"})


def test_valid_token_yields_its_subject():
    assert user_id_from_token(mint()) == USER


def test_subject_not_the_configured_default():
    """The token is the source of user_id, not any configured identity."""
    other = "00000000-0000-4000-8000-0000000000ff"
    assert user_id_from_token(mint(sub=other)) == other
    assert "default_user_id" not in Settings.model_fields


@pytest.mark.parametrize(
    "header",
    [None, "", "Bearer", "Bearer ", "Basic abc", "abc.def.ghi"],
)
def test_bad_authorization_headers_are_rejected(header):
    with pytest.raises(HTTPException) as e:
        bearer_token(header)
    assert e.value.status_code == 401


@pytest.mark.parametrize(
    "token_kwargs",
    [
        {"secret": "a-different-secret-entirely"},  # forged
        {"exp_delta": -60},  # expired
        {"drop_sub": True},  # no subject
        {"aud": "anon"},  # wrong audience
    ],
)
def test_bad_tokens_are_rejected(token_kwargs):
    with pytest.raises(HTTPException) as e:
        user_id_from_token(mint(**token_kwargs))
    assert e.value.status_code == 401
    assert e.value.headers["WWW-Authenticate"] == "Bearer"


def test_none_algorithm_is_rejected():
    """An unsigned token must never authenticate."""
    forged = jwt.encode({"sub": USER, "aud": "authenticated"}, None, algorithm="none")
    with pytest.raises(HTTPException) as e:
        user_id_from_token(forged)
    assert e.value.status_code == 401


def test_missing_secret_is_a_server_error_not_an_open_door(monkeypatch):
    """No secret configured must fail closed, not authenticate everyone."""
    token = mint()
    monkeypatch.setattr(auth.settings, "supabase_jwt_secret", "")
    with pytest.raises(HTTPException) as e:
        user_id_from_token(token)
    assert e.value.status_code == 500
