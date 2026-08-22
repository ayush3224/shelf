"""Supabase JWT verification (UC41).

Single user, but the token is the only thing that decides `user_id` — the
service never falls back to a configured default. `/health` is the one
unauthenticated surface; everything else is fail-closed.
"""

from typing import Optional

import jwt
from fastapi import HTTPException, Request, status

from backend.config import settings

# Paths reachable without a bearer token. Keep this list short.
PUBLIC_PATHS: frozenset[str] = frozenset({"/health"})

_ALGORITHMS = ["HS256"]


def _unauthorized(detail: str) -> HTTPException:
    """Build a 401 with the bearer challenge header."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def bearer_token(authorization: Optional[str]) -> str:
    """Pull the raw token out of an Authorization header value.

    Args:
        authorization: Raw `Authorization` header, or None if absent.

    Returns:
        The bearer token.

    Raises:
        HTTPException: 401 if the header is missing or malformed.
    """
    if not authorization:
        raise _unauthorized("Missing Authorization header")

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _unauthorized("Authorization header must be 'Bearer <token>'")

    return token.strip()


def user_id_from_token(token: str) -> str:
    """Verify a Supabase access token and return its `sub` claim.

    Args:
        token: Encoded JWT signed with SUPABASE_JWT_SECRET.

    Returns:
        The authenticated user's UUID.

    Raises:
        HTTPException: 401 if the token is invalid, expired, or has no `sub`.
    """
    if not settings.supabase_jwt_secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="SUPABASE_JWT_SECRET is not configured",
        )

    options = {"require": ["sub", "exp"]}
    audience = settings.supabase_jwt_aud or None
    if audience is None:
        options["verify_aud"] = False

    try:
        claims = jwt.decode(
            token,
            settings.supabase_jwt_secret,
            algorithms=_ALGORITHMS,
            audience=audience,
            options=options,
        )
    except jwt.ExpiredSignatureError:
        raise _unauthorized("Token expired")
    except jwt.InvalidTokenError as e:
        raise _unauthorized(f"Invalid token: {e}")

    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        raise _unauthorized("Token has no subject")

    return sub


def authenticate(request: Request) -> str:
    """Authenticate a request from its Authorization header.

    Args:
        request: The incoming request.

    Returns:
        The authenticated user's UUID.

    Raises:
        HTTPException: 401 on a missing or invalid token.
    """
    return user_id_from_token(bearer_token(request.headers.get("authorization")))


def current_user_id(request: Request) -> str:
    """FastAPI dependency: the user_id the auth middleware resolved.

    Args:
        request: The incoming request.

    Returns:
        The authenticated user's UUID.

    Raises:
        HTTPException: 401 if the middleware did not authenticate this request.
    """
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise _unauthorized("Not authenticated")
    return user_id
