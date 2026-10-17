"""Civix envelope decode.

EVR responses are ``{"upload": "<base64>"}`` and the payload may be JSON, CSV,
or ZIP bytes. ENR responses reuse that ``upload`` wrapper for some documents
and base64-JSON sections for others. Both paths share :func:`decode_base64_bytes`.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

_SECTION_DECODE_ERRORS = (binascii.Error, UnicodeDecodeError, ValueError)


def decode_base64_bytes(value: str | None) -> bytes:
    """Decode a Civix base64 field. Empty input is an empty payload."""
    if not value:
        return b""
    try:
        return base64.b64decode(value)
    except (binascii.Error, ValueError) as exc:
        msg = "Civix upload payload was not valid base64"
        raise ValueError(msg) from exc


def _decode_envelope(response: Any) -> bytes:
    """Decode the universal Civix base64 envelope to raw bytes.

    Every Civix EVR endpoint returns a JSON body of the form::

        {"upload": "<base64-encoded payload>"}

    Civix sometimes returns HTTP 200 with an empty body for per-county rosters
    that have no voters on that date. Treat that as an empty payload.

    Raises:
        KeyError: If the response JSON lacks the ``"upload"`` key.
        ValueError: If the response body is non-empty but not valid JSON.
    """
    body = (response.text or "").strip()
    if not body:
        return b""
    try:
        envelope = response.json()
    except ValueError as exc:
        msg = "Civix response was not valid JSON"
        raise ValueError(msg) from exc
    upload = envelope.get("upload")
    if upload is None:
        raise KeyError("upload")
    if not isinstance(upload, str):
        msg = "Civix upload field was not a string"
        raise ValueError(msg)
    return decode_base64_bytes(upload)


def decode_base64_json(value: str | None) -> Any:
    """Decode a base64 JSON field. Empty input decodes to ``{}``."""
    if not value:
        return {}
    raw = decode_base64_bytes(value)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        msg = "Civix upload payload was not JSON"
        raise ValueError(msg) from exc


def decode_upload_payload(raw_payload: dict[str, Any]) -> dict[str, Any]:
    """Decode objects that wrap base64 JSON in ``upload``."""
    upload = raw_payload.get("upload")
    try:
        decoded = decode_base64_json(upload if isinstance(upload, str) else None)
    except ValueError:
        return {}
    if not isinstance(decoded, dict):
        return {}
    return decoded


def decode_election_constants(raw_payload: dict[str, Any]) -> dict[str, Any]:
    """Decode an electionConstants wrapper payload into structured JSON."""
    return decode_upload_payload(raw_payload)


def decode_election_payload(raw_payload: dict[str, Any]) -> dict[str, Any]:
    """Decode ``/s3/enr/election/{id}`` where non-Version sections are base64 JSON.

    A section that is not valid base64 JSON becomes ``{}`` so one bad section
    does not drop the rest of the document.
    """
    decoded: dict[str, Any] = {}
    for key, value in raw_payload.items():
        if key == "Version":
            decoded[key] = value
            continue
        if isinstance(value, str):
            try:
                decoded[key] = decode_base64_json(value)
            except _SECTION_DECODE_ERRORS:
                decoded[key] = {}
        else:
            decoded[key] = value
    return decoded


def decode_county_info_payload(raw_payload: dict[str, Any]) -> dict[str, Any]:
    """Decode ``/s3/enr/election/countyInfo/{id}`` upload payload."""
    return decode_upload_payload(raw_payload)
