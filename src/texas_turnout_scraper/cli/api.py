"""Preview read API keys: generate, extend, list, revoke."""

from __future__ import annotations

import datetime as dt
import sys
from typing import Annotated

import httpx
import typer

from ..consumer_token_admin import (
    ConsumerTokenAdmin,
    TokenNoExpiryError,
    TokenNotFoundError,
    TokenRevokedError,
)
from ..consumer_tokens import DEFAULT_KEY_DAYS, MAX_KEY_DAYS, SCOPE_AGGREGATES
from ..early_vote_sync import EarlyVoteSyncError
from ..onepassword_keys import (
    OnePasswordError,
    OnePasswordKeyStore,
    SubprocessOnePasswordRunner,
)
from ._typer_apps import api_app

EXIT_SERVICE = 1
EXIT_USAGE = 2

_CLOUDFLARE_ERRORS = (EarlyVoteSyncError, httpx.HTTPError)


def _admin() -> ConsumerTokenAdmin:
    try:
        return ConsumerTokenAdmin.from_env()
    except EarlyVoteSyncError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=EXIT_SERVICE) from exc


def _key_store() -> OnePasswordKeyStore:
    return OnePasswordKeyStore(SubprocessOnePasswordRunner())


def _stdin_is_tty() -> bool:
    return bool(sys.stdin.isatty())


def _fail(message: str, code: int) -> typer.Exit:
    typer.echo(f"Error: {message}", err=True)
    return typer.Exit(code=code)


def validate_name(raw: str) -> str:
    name = raw.strip()
    if not name:
        raise ValueError("name must not be empty")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise ValueError("name must not contain control characters")
    return name


def validate_email(raw: str) -> str:
    email = raw.strip()
    local, sep, domain = email.partition("@")
    if (
        not sep
        or "@" in domain
        or not local
        or "." not in domain
        or domain.startswith(".")
        or domain.endswith(".")
        or any(ch.isspace() for ch in email)
    ):
        raise ValueError("email address is not valid")
    return email


def _resolve(value: str | None, option: str, prompt_text: str, *, interactive: bool) -> str:
    if value is not None:
        return value
    if not interactive:
        raise _fail(f"{option} is required (no prompt: non-interactive or no TTY)", EXIT_USAGE)
    return str(typer.prompt(prompt_text))


@api_app.command("generate")
def api_generate(
    name: Annotated[str | None, typer.Option("--name", help="Who the key is for")] = None,
    email: Annotated[
        str | None, typer.Option("--email", help="Contact email (1Password only)")
    ] = None,
    days: Annotated[
        int, typer.Option("--days", min=1, max=MAX_KEY_DAYS, help="Days until the key expires")
    ] = DEFAULT_KEY_DAYS,
    scope: Annotated[
        str,
        typer.Option(
            "--scope",
            help="aggregates (counts only, default) or roster (voter rows, names and VUIDs)",
        ),
    ] = SCOPE_AGGREGATES,
    show: Annotated[bool, typer.Option("--show", help="Print the key once on stdout")] = False,
    non_interactive: Annotated[
        bool, typer.Option("--non-interactive", help="Never prompt; fail with exit 2")
    ] = False,
) -> None:
    """Generate an expiring API key and store it in 1Password."""
    interactive = not non_interactive and _stdin_is_tty()
    raw_name = _resolve(name, "--name", "Name", interactive=interactive)
    raw_email = _resolve(email, "--email", "Email address", interactive=interactive)
    try:
        clean_name = validate_name(raw_name)
        clean_email = validate_email(raw_email)
    except ValueError as exc:
        raise _fail(str(exc), EXIT_USAGE) from exc

    admin = _admin()
    try:
        try:
            issued = admin.issue(clean_name, scope=scope, days=days)
        except ValueError as exc:
            raise _fail(str(exc), EXIT_USAGE) from exc

        store = _key_store()
        try:
            item = store.create(
                name=clean_name,
                email=clean_email,
                api_key=issued.plaintext,
                token_id=issued.token_id,
                expires_date=issued.expires_at[:10],
            )
        except OnePasswordError as exc:
            raise _fail(f"{exc} Nothing was written to Cloudflare.", EXIT_SERVICE) from exc

        committed = False
        try:
            admin.store(issued)
            committed = True
        except _CLOUDFLARE_ERRORS as exc:
            raise _fail(
                f"Cloudflare write failed ({type(exc).__name__}); "
                "the 1Password item was archived (best effort).",
                EXIT_SERVICE,
            ) from exc
        finally:
            if not committed:
                _rollback(admin, store, item.item_id, issued.token_id)
    finally:
        admin.close()

    typer.echo(f"token_id={issued.token_id}")
    typer.echo(f"name={clean_name}")
    typer.echo(f"scope={issued.scope}")
    typer.echo(f"expires_at={issued.expires_at}")
    typer.echo(f"1password_item={item.title} ({item.item_id})")
    if show:
        typer.echo(f"key={issued.plaintext}")


def _rollback(
    admin: ConsumerTokenAdmin, store: OnePasswordKeyStore, item_id: str, token_id: str
) -> None:
    try:
        store.archive(item_id)
    except OnePasswordError:
        typer.echo(
            f"Warning: could not archive the 1Password item {item_id}; remove it by hand "
            f"(token_id={token_id}).",
            err=True,
        )
    try:
        admin.revoke(token_id)
    except _CLOUDFLARE_ERRORS:
        typer.echo(
            f"Warning: could not revoke the partial Cloudflare token; run "
            f"`tx-turnout api revoke {token_id}`.",
            err=True,
        )


@api_app.command("extend")
def api_extend(
    token_id: Annotated[str, typer.Argument(help="token_id from generate or list")],
    days: Annotated[
        int, typer.Option("--days", min=1, max=MAX_KEY_DAYS, help="Days to add")
    ] = DEFAULT_KEY_DAYS,
    set_expiry: Annotated[
        bool,
        typer.Option(
            "--set-expiry",
            help="Allow putting an expiry on a key that currently never expires",
        ),
    ] = False,
) -> None:
    """Extend a key: max(now, current expiry) + N days (D1, KV, 1Password)."""
    admin = _admin()
    try:
        new_expiry = admin.extend(token_id, days, set_expiry=set_expiry)
    except TokenNotFoundError as exc:
        raise _fail("unknown token_id", EXIT_SERVICE) from exc
    except TokenRevokedError as exc:
        raise _fail("token is revoked and cannot be extended", EXIT_SERVICE) from exc
    except TokenNoExpiryError as exc:
        raise _fail(
            "token never expires; pass --set-expiry to put an expiry on it",
            EXIT_SERVICE,
        ) from exc
    except _CLOUDFLARE_ERRORS as exc:
        raise _fail(f"Cloudflare write failed ({type(exc).__name__})", EXIT_SERVICE) from exc
    finally:
        admin.close()

    typer.echo(f"token_id={token_id}")
    typer.echo(f"expires_at={new_expiry}")
    store = _key_store()
    try:
        item_id = store.find_by_token_id(token_id)
        if item_id is None:
            typer.echo(
                f"Warning: no 1Password item is tagged token:{token_id}; "
                "Cloudflare was extended, update the item by hand.",
                err=True,
            )
            return
        store.update_expiry(item_id, new_expiry[:10])
    except OnePasswordError as exc:
        raise _fail(
            f"{exc} Cloudflare was extended to {new_expiry[:10]} but the 1Password item was not "
            f"updated: set its Expires field to {new_expiry[:10]} by hand. "
            "Do not run extend again, it would add the days a second time.",
            EXIT_SERVICE,
        ) from exc
    typer.echo(f"1password_item={item_id}")


@api_app.command("list")
def api_list() -> None:
    """List keys: id, status, scope, created, expires, label (no secrets)."""
    admin = _admin()
    try:
        rows = admin.list_tokens()
    except _CLOUDFLARE_ERRORS as exc:
        raise _fail(f"Cloudflare read failed ({type(exc).__name__})", EXIT_SERVICE) from exc
    finally:
        admin.close()
    if not rows:
        typer.echo("No API keys.")
        return
    now = dt.datetime.now(tz=dt.timezone.utc)
    for row in rows:
        typer.echo(
            f"{row.token_id}\t{row.status(now)}\t{row.scope}\t{row.created_at}\t"
            f"{row.expires_at or 'never'}\t{row.label}",
        )


@api_app.command("revoke")
def api_revoke(
    token_id: Annotated[str, typer.Argument(help="token_id from generate or list")],
) -> None:
    """Revoke a key (D1 + KV). The 1Password item is left alone."""
    admin = _admin()
    try:
        ok = admin.revoke(token_id)
    except _CLOUDFLARE_ERRORS as exc:
        raise _fail(f"Cloudflare write failed ({type(exc).__name__})", EXIT_SERVICE) from exc
    finally:
        admin.close()
    if not ok:
        raise _fail("unknown token_id", EXIT_SERVICE)
    typer.echo(f"revoked {token_id}")
