"""Store issued consumer API keys in 1Password through the ``op`` CLI.

The plaintext key travels to ``op`` on stdin inside a JSON item template, never in argv.
Errors carry no key, name or email. ``op`` auth failures raise :class:`OnePasswordAuthError`
so the CLI can tell the user to run ``op signin`` once instead of retrying.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

VAULT_ID = "qxyxd4oqdy5mpmg4t4qh3bfflm"
VAULT_ENV = "TX_TURNOUT_OP_VAULT"
ITEM_TITLE_PREFIX = "texas-turnout-scraper: "
BASE_TAG = "texas-turnout-scraper"
TOKEN_TAG_PREFIX = "token:"
API_BASE_URL = "https://ev-api-preview.abstractdata.io"
API_BASE_URL_ENV = "TX_TURNOUT_API_BASE_URL"

FIELD_CREDENTIAL_ID = "credential"
FIELD_API_KEY = "API key"
FIELD_NAME = "Name"
FIELD_EMAIL = "Email Address"
FIELD_EXPIRES = "Expires"
FIELD_TOKEN_ID = "Token ID"
FIELD_INSTRUCTIONS = "Instructions"

_OP_TIMEOUT_SECONDS = 60
_AUTH_MARKERS = (
    "not signed in",
    "no accounts configured",
    "session expired",
    "authorization prompt",
    "authorization timeout",
)

INSTRUCTIONS_TEMPLATE = """\
This is an Abstract Data texas-turnout-scraper API key for the early-vote read API.

Paste these lines into the .env file of your project, replacing each <...> with the value
of that field in this item, and keep .env out of git:
  TX_TURNOUT_API_KEY=<the "API key" field>
  TX_TURNOUT_API_NAME=<the "Name" field>
  TX_TURNOUT_API_EMAIL=<the "Email Address" field>

For an agent: send the key to the read API at {base_url} as either
  Authorization: Bearer $TX_TURNOUT_API_KEY
  X-API-Key: $TX_TURNOUT_API_KEY
The API also sits behind Cloudflare Access. Its service-token headers (CF-Access-Client-Id and
CF-Access-Client-Secret) are provided separately by Abstract Data.

Expiry: this key expires on {expires}. Abstract Data can extend it.

Never commit this key. Never paste it into a chat or a log.
"""


class OnePasswordError(RuntimeError):
    """A 1Password CLI call failed. The message never holds a key, name or email."""


class OnePasswordAuthError(OnePasswordError):
    """``op`` is missing or not signed in. Run ``op signin`` and retry once."""


class OnePasswordRunner(Protocol):
    def run(self, args: Sequence[str], stdin: str | None = None) -> str:
        """Run ``op <args>`` and return stdout. Raises :class:`OnePasswordError`."""
        ...


class SubprocessOnePasswordRunner:
    """Runs the real ``op`` binary."""

    def run(self, args: Sequence[str], stdin: str | None = None) -> str:
        try:
            completed = subprocess.run(  # noqa: S603 - fixed binary, argv holds no secret
                ["op", *args],
                input=stdin,
                capture_output=True,
                text=True,
                timeout=_OP_TIMEOUT_SECONDS,
                check=False,
            )
        except FileNotFoundError as exc:
            raise OnePasswordAuthError(
                "1Password CLI `op` was not found. Install it, then run `op signin`."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise OnePasswordAuthError(
                "1Password CLI timed out. Run `op signin` in an interactive terminal, then retry."
            ) from exc
        if completed.returncode != 0:
            stderr = completed.stderr.lower()
            if any(marker in stderr for marker in _AUTH_MARKERS):
                raise OnePasswordAuthError(
                    "1Password CLI is not signed in. Run `op signin` in an interactive "
                    "terminal, then retry."
                )
            raise OnePasswordError(f"op exited with status {completed.returncode}")
        return completed.stdout


@dataclass(frozen=True)
class OnePasswordItem:
    item_id: str
    title: str


def item_title(name: str) -> str:
    return f"{ITEM_TITLE_PREFIX}{name}"


def token_tag(token_id: str) -> str:
    return f"{TOKEN_TAG_PREFIX}{token_id}"


def api_base_url() -> str:
    return os.environ.get(API_BASE_URL_ENV, "").strip().rstrip("/") or API_BASE_URL


def vault_id() -> str:
    return os.environ.get(VAULT_ENV, "").strip() or VAULT_ID


def render_instructions(expires_date: str) -> str:
    return INSTRUCTIONS_TEMPLATE.format(base_url=api_base_url(), expires=expires_date)


def build_item_template(
    *,
    name: str,
    email: str,
    api_key: str,
    token_id: str,
    expires_date: str,
) -> dict[str, object]:
    """JSON item template for ``op item create -``. Holds the key; pass it on stdin only."""
    return {
        "title": item_title(name),
        "category": "API_CREDENTIAL",
        "tags": [BASE_TAG, token_tag(token_id)],
        "fields": [
            {
                "id": FIELD_CREDENTIAL_ID,
                "type": "CONCEALED",
                "label": FIELD_API_KEY,
                "value": api_key,
            },
            {"id": "name", "type": "STRING", "label": FIELD_NAME, "value": name},
            {"id": "email_address", "type": "EMAIL", "label": FIELD_EMAIL, "value": email},
            {"id": "expires_on", "type": "DATE", "label": FIELD_EXPIRES, "value": expires_date},
            {"id": "token_id", "type": "STRING", "label": FIELD_TOKEN_ID, "value": token_id},
            {
                "id": "instructions",
                "type": "STRING",
                "label": FIELD_INSTRUCTIONS,
                "value": render_instructions(expires_date),
            },
        ],
    }


class OnePasswordKeyStore:
    """Create, find and update the 1Password item that holds one issued key."""

    def __init__(self, runner: OnePasswordRunner, vault: str | None = None) -> None:
        self._runner = runner
        self._vault = vault or vault_id()

    def create(
        self,
        *,
        name: str,
        email: str,
        api_key: str,
        token_id: str,
        expires_date: str,
    ) -> OnePasswordItem:
        template = build_item_template(
            name=name,
            email=email,
            api_key=api_key,
            token_id=token_id,
            expires_date=expires_date,
        )
        out = self._runner.run(
            ["item", "create", "-", "--vault", self._vault, "--format", "json"],
            stdin=json.dumps(template),
        )
        item_id = _item_id_from(out)
        if not item_id:
            raise OnePasswordError("op did not return an item id")
        return OnePasswordItem(item_id=item_id, title=item_title(name))

    def archive(self, item_id: str) -> None:
        self._runner.run(["item", "delete", item_id, "--archive", "--vault", self._vault])

    def find_by_token_id(self, token_id: str) -> str | None:
        out = self._runner.run(
            [
                "item",
                "list",
                "--vault",
                self._vault,
                "--tags",
                token_tag(token_id),
                "--format",
                "json",
            ]
        )
        try:
            items = json.loads(out) if out.strip() else []
        except json.JSONDecodeError as exc:
            raise OnePasswordError("op returned unreadable item list") from exc
        if not isinstance(items, list):
            return None
        for item in items:
            found = item.get("id") if isinstance(item, dict) else None
            if isinstance(found, str) and found:
                return found
        return None

    def update_expiry(self, item_id: str, expires_date: str) -> None:
        """Set Expires and re-render Instructions. Neither value is secret."""
        self._runner.run(
            [
                "item",
                "edit",
                item_id,
                "--vault",
                self._vault,
                f"{FIELD_EXPIRES}[date]={expires_date}",
                f"{FIELD_INSTRUCTIONS}[text]={render_instructions(expires_date)}",
            ]
        )


def _item_id_from(output: str) -> str:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise OnePasswordError("op returned unreadable output") from exc
    item_id = payload.get("id") if isinstance(payload, dict) else None
    return item_id if isinstance(item_id, str) else ""
