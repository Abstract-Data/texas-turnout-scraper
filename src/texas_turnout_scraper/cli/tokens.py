"""Preview consumer API token management (mint / list / revoke)."""

from __future__ import annotations

from typing import Annotated

import typer

from ..consumer_token_admin import ConsumerTokenAdmin
from ..early_vote_sync import EarlyVoteSyncError
from ._typer_apps import tokens_app


def _admin() -> ConsumerTokenAdmin:
    try:
        return ConsumerTokenAdmin.from_env()
    except EarlyVoteSyncError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@tokens_app.command("mint")
def tokens_mint(
    label: Annotated[
        str,
        typer.Option("--label", "-l", help="Human-readable label for this consumer"),
    ] = "",
) -> None:
    """Mint a consumer token (plaintext shown once on stdout)."""
    admin = _admin()
    try:
        token_id, plaintext = admin.mint(label)
    except EarlyVoteSyncError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        admin.close()
    typer.echo(f"token_id={token_id}")
    typer.echo(f"label={label}")
    typer.echo(plaintext)


@tokens_app.command("list")
def tokens_list() -> None:
    """List minted tokens (hashes and secrets are never shown)."""
    admin = _admin()
    try:
        rows = admin.list_tokens()
    except EarlyVoteSyncError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        admin.close()
    if not rows:
        typer.echo("No consumer tokens.")
        return
    for row in rows:
        status = "revoked" if row.revoked_at else "active"
        typer.echo(
            f"{row.token_id}\t{status}\t{row.created_at}\t{row.label}",
        )


@tokens_app.command("revoke")
def tokens_revoke(
    token_id: Annotated[str, typer.Argument(help="token_id from mint or list")],
) -> None:
    """Revoke a consumer token (D1 + KV)."""
    admin = _admin()
    try:
        ok = admin.revoke(token_id)
    except EarlyVoteSyncError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        admin.close()
    if not ok:
        typer.echo(f"Error: unknown token_id {token_id!r}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"revoked {token_id}")
