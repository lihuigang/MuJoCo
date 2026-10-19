"""Bearer-token authentication with project identity derived server-side."""

from __future__ import annotations

import hmac

from ..core.errors import PermissionDenied, Unauthorized
from .config import Settings, TokenPolicy


def authenticate(settings: Settings, authorization: str | None) -> TokenPolicy:
    if not authorization or not authorization.startswith("Bearer "):
        raise Unauthorized("Provide a project token as 'Authorization: Bearer <token>'")
    supplied = authorization.removeprefix("Bearer ").strip()
    if not supplied:
        raise Unauthorized("Empty bearer token")
    matched = None
    for policy in settings.tokens:
        if hmac.compare_digest(supplied.encode("utf-8"), policy.token.encode("utf-8")):
            matched = policy
    if matched is None:
        raise Unauthorized("Invalid project token")
    return matched


def require_scope(policy: TokenPolicy, scope: str) -> None:
    if not policy.allows(scope):
        raise PermissionDenied(f"project {policy.project_id!r} lacks the {scope!r} scope")
