"""Cloudflare Access service-token check.

Header names are the read-client constants. Preview reads require both.
Access is the enforcement point once the Terraform PR attaches a Service
Auth policy. Until then the Worker fails closed: missing or mismatched
headers get no body.

https://developers.cloudflare.com/cloudflare-one/access-controls/service-credentials/service-tokens/
"""

from __future__ import annotations

import hmac
from collections.abc import Mapping

from texas_turnout_scraper.civix.read_client import (
    CF_ACCESS_CLIENT_ID_HEADER,
    CF_ACCESS_CLIENT_SECRET_HEADER,
)


def _header(headers: Mapping[str, str], name: str) -> str:
    folded = name.lower()
    for key, value in headers.items():
        if key.lower() == folded:
            return value
    return ""


def access_authorized(
    headers: Mapping[str, str],
    *,
    client_id: str,
    client_secret: str,
) -> bool:
    """True only when both configured credentials match. Empty config is closed."""
    if client_id == "" or client_secret == "":
        return False
    presented_id = _header(headers, CF_ACCESS_CLIENT_ID_HEADER)
    presented_secret = _header(headers, CF_ACCESS_CLIENT_SECRET_HEADER)
    id_ok = hmac.compare_digest(presented_id, client_id)
    secret_ok = hmac.compare_digest(presented_secret, client_secret)
    return id_ok and secret_ok
