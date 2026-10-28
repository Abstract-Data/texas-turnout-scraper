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

from .consumer_tokens import SCOPE_AGGREGATES, SCOPE_ROSTER, parse_scope

VAULT_ID = "qxyxd4oqdy5mpmg4t4qh3bfflm"
VAULT_ENV = "TX_TURNOUT_OP_VAULT"
ITEM_TITLE_PREFIX = "texas-turnout-scraper: "
BASE_TAG = "texas-turnout-scraper"
TOKEN_TAG_PREFIX = "token:"
API_BASE_URL = "https://ev-api-preview.abstractdata.io"
API_BASE_URL_ENV = "TX_TURNOUT_API_BASE_URL"

FIELD_CREDENTIAL_ID = "credential"
FIELD_API_KEY = "API key"
FIELD_ENDPOINT = "Endpoint"
FIELD_SCOPE = "Scope"
FIELD_NAME = "Name"
FIELD_EMAIL = "Email Address"
FIELD_EXPIRES = "Expires"
FIELD_TOKEN_ID = "Token ID"
FIELD_INSTRUCTIONS = "Instructions"
FIELD_NOTES = "notesPlain"

_OP_TIMEOUT_SECONDS = 60
_AUTH_MARKERS = (
    "not signed in",
    "no accounts configured",
    "session expired",
    "authorization prompt",
    "authorization timeout",
)

INSTRUCTIONS_TEMPLATE = """\
This note is the handoff for an Abstract Data texas-turnout-scraper API key.
Give an agent this note and this 1Password item. The agent reads the fields below.
It does not copy the API key into a chat, a log, a commit, or this note.

Fields on this item:
  API key — concealed. Send it on every request. Never print it.
  Endpoint — {endpoint}
  Scope — {scope}
  Name — who the key is for.
  Email Address — contact. It is stored only in this item.
  Expires — {expires}. Abstract Data can extend it.
  Token ID — identifies the key. It is not the API key.

.env lines, if the project loads one. Keep .env out of git. Copy each value from this item:
  TX_TURNOUT_API_KEY=<the "API key" field>
  TX_TURNOUT_API_NAME=<the "Name" field>
  TX_TURNOUT_API_EMAIL=<the "Email Address" field>
  TX_TURNOUT_API_ENDPOINT=<the "Endpoint" field>

Call the API:
  1. First request: GET {endpoint}
     Optional filter: ?q= followed by part of a source_election_id.
  2. Send the API key as either header:
       Authorization: Bearer $TX_TURNOUT_API_KEY
       X-API-Key: $TX_TURNOUT_API_KEY
     From this item, that value is the API key field, not the Token ID.
  3. Also send Cloudflare Access headers. They are not in this item:
       CF-Access-Client-Id
       CF-Access-Client-Secret
     Abstract Data provides them separately. Without them the edge stops the
     request before the API key is checked.
  4. GET {endpoint} returns JSON with an elections array. Each election has
     source_election_id, first_ev_date, last_ev_date, total_rows, and days
     (ev_date, rows, sha256, byte_length). Those are counts. There are no voter names.
  5. One election: GET {base_url}/elections/<source_election_id>
  6. Counts for that election, with this same key:
       GET {base_url}/elections/<source_election_id>/aggregates/totals
       GET {base_url}/elections/<source_election_id>/aggregates/county
       GET {base_url}/elections/<source_election_id>/aggregates/by-method
       GET {base_url}/elections/<source_election_id>/aggregates/by-day
       GET {base_url}/elections/<source_election_id>/aggregates/county-by-day
     Add ?ev_date=YYYY-MM-DD for that day only. Leave it off for the cumulative total.
       GET {base_url}/elections/<source_election_id>/geo/precincts
     returns precinct points and boxes, not voters.
{scope_steps}
  7. 401 means the Access headers or the API key was rejected. 403 on a roster
     route means this key's scope is aggregates. Do not invent another key.

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


def entrypoint_url() -> str:
    """Catalog route an agent calls first. Counts only; no voter rows."""
    return f"{api_base_url()}/elections"


def vault_id() -> str:
    return os.environ.get(VAULT_ENV, "").strip() or VAULT_ID


def _scope_steps(scope: str, base_url: str) -> str:
    if scope == SCOPE_ROSTER:
        return (
            "  Scope roster: this key can also download voter rows. The CSV includes\n"
            "  ID_VOTER and VOTER_NAME. Do not log names or VUIDs.\n"
            f"    GET {base_url}/elections/<source_election_id>/roster\n"
            f"    GET {base_url}/elections/<source_election_id>/roster/<YYYY-MM-DD>\n"
            "  An aggregates key gets 403 on those routes."
        )
    return (
        "  Scope aggregates: this key reads counts only. Do not call a /roster route.\n"
        "  It returns 403. Voter rows need a different key whose Scope field is roster."
    )


def render_instructions(expires_date: str, *, scope: str = SCOPE_AGGREGATES) -> str:
    """Agent handoff note. The API key is not included."""
    clean_scope = parse_scope(scope)
    base_url = api_base_url()
    return INSTRUCTIONS_TEMPLATE.format(
        base_url=base_url,
        endpoint=entrypoint_url(),
        expires=expires_date,
        scope=clean_scope,
        scope_steps=_scope_steps(clean_scope, base_url),
    )


def build_item_template(
    *,
    name: str,
    email: str,
    api_key: str,
    token_id: str,
    expires_date: str,
    scope: str = SCOPE_AGGREGATES,
) -> dict[str, object]:
    """JSON item template for ``op item create -``. Holds the key; pass it on stdin only."""
    note = render_instructions(expires_date, scope=scope)
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
            {
                "id": "endpoint",
                "type": "URL",
                "label": FIELD_ENDPOINT,
                "value": entrypoint_url(),
            },
            {
                "id": "scope",
                "type": "STRING",
                "label": FIELD_SCOPE,
                "value": parse_scope(scope),
            },
            {"id": "name", "type": "STRING", "label": FIELD_NAME, "value": name},
            {"id": "email_address", "type": "EMAIL", "label": FIELD_EMAIL, "value": email},
            {"id": "expires_on", "type": "DATE", "label": FIELD_EXPIRES, "value": expires_date},
            {"id": "token_id", "type": "STRING", "label": FIELD_TOKEN_ID, "value": token_id},
            {
                "id": "instructions",
                "type": "STRING",
                "label": FIELD_INSTRUCTIONS,
                "value": note,
            },
            {
                "id": FIELD_NOTES,
                "type": "STRING",
                "purpose": "NOTES",
                "label": FIELD_NOTES,
                "value": note,
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
        scope: str = SCOPE_AGGREGATES,
    ) -> OnePasswordItem:
        template = build_item_template(
            name=name,
            email=email,
            api_key=api_key,
            token_id=token_id,
            expires_date=expires_date,
            scope=scope,
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

    def update_expiry(
        self, item_id: str, expires_date: str, *, scope: str = SCOPE_AGGREGATES
    ) -> None:
        """Set Expires and re-render the agent note. Neither value is secret."""
        note = render_instructions(expires_date, scope=scope)
        clean_scope = parse_scope(scope)
        self._runner.run(
            [
                "item",
                "edit",
                item_id,
                "--vault",
                self._vault,
                f"{FIELD_EXPIRES}[date]={expires_date}",
                f"{FIELD_ENDPOINT}[url]={entrypoint_url()}",
                f"{FIELD_SCOPE}[text]={clean_scope}",
                f"{FIELD_INSTRUCTIONS}[text]={note}",
                f"{FIELD_NOTES}[text]={note}",
            ]
        )


def _item_id_from(output: str) -> str:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise OnePasswordError("op returned unreadable output") from exc
    item_id = payload.get("id") if isinstance(payload, dict) else None
    return item_id if isinstance(item_id, str) else ""
