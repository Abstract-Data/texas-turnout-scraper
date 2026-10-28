"""``tx-turnout api`` commands against fake Cloudflare and a fake 1Password CLI."""

from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from urllib.parse import urlparse

import pytest
from typer.testing import CliRunner

from texas_turnout_scraper.cli import api as api_cli
from texas_turnout_scraper.cli._typer_apps import app
from texas_turnout_scraper.consumer_tokens import hash_token
from texas_turnout_scraper.onepassword_keys import (
    OnePasswordAuthError,
    OnePasswordError,
    SubprocessOnePasswordRunner,
)

from .fake_cloudflare import FakeCloudflare, FakeOp, signed_out

NAME = "Ada Lovelace"
EMAIL = "ada.private@example.org"


@dataclass
class Env:
    cf: FakeCloudflare
    op: FakeOp
    events: list[str]
    tty: list[bool]

    def invoke(self, *args: str, input: str | None = None):  # noqa: A002
        return CliRunner().invoke(app, ["api", *args], input=input)

    def items(self) -> list[dict[str, object]]:
        return list(self.op.items.values())

    def field(self, label: str) -> str:
        template = self.items()[0]
        for field in template["fields"]:  # type: ignore[attr-defined]
            if field["label"] == label:
                return field["value"]
        raise KeyError(label)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    events: list[str] = []
    cf = FakeCloudflare(events)
    op = FakeOp(events)
    tty = [True]
    monkeypatch.setattr(api_cli, "_admin", cf.admin)
    monkeypatch.setattr(api_cli, "_key_store", op.store)
    monkeypatch.setattr(api_cli, "_stdin_is_tty", lambda: tty[0])
    yield Env(cf=cf, op=op, events=events, tty=tty)


def _leaks(text: str, *secrets: str) -> list[str]:
    return [s for s in secrets if s in text]


def test_generate_prompts_for_missing_name_and_email_on_a_tty(env: Env) -> None:
    result = env.invoke("generate", input=f"{NAME}\n{EMAIL}\n")
    assert result.exit_code == 0, result.output
    assert env.field("Name") == NAME
    assert env.field("Email Address") == EMAIL


def test_generate_prompts_only_for_the_missing_option(env: Env) -> None:
    result = env.invoke("generate", "--name", NAME, input=f"{EMAIL}\n")
    assert result.exit_code == 0, result.output
    assert env.field("Email Address") == EMAIL


def test_non_interactive_missing_option_exits_2_and_writes_nothing(env: Env) -> None:
    result = env.invoke("generate", "--name", NAME, "--non-interactive")
    assert result.exit_code == 2
    assert "--email" in result.output
    assert env.events == []


def test_no_tty_missing_option_exits_2_without_prompting(env: Env) -> None:
    env.tty[0] = False
    result = env.invoke("generate", "--email", EMAIL, input="ignored\n")
    assert result.exit_code == 2
    assert "--name" in result.output
    assert env.events == []


def test_both_options_given_never_prompts(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_prompt(*_a: object, **_k: object) -> str:
        raise AssertionError("prompted")

    monkeypatch.setattr(api_cli.typer, "prompt", no_prompt)
    result = env.invoke("generate", "--name", NAME, "--email", EMAIL)
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize(
    "email",
    [
        "",
        "no-at-sign",
        "a@b",
        "@example.org",
        "a@@example.org",
        "a b@example.org",
        "a@.org",
        "a@org.",
    ],
)
def test_bad_email_is_rejected_with_exit_2(env: Env, email: str) -> None:
    result = env.invoke("generate", "--name", NAME, "--email", email)
    assert result.exit_code == 2
    assert env.events == []
    assert not _leaks(result.output, email) or email == ""


@pytest.mark.parametrize("name", ["", "   ", "bad\x07name", "tab\tname"])
def test_bad_name_is_rejected_with_exit_2(env: Env, name: str) -> None:
    result = env.invoke("generate", "--name", name, "--email", EMAIL)
    assert result.exit_code == 2
    assert env.events == []


def test_default_output_hides_key_and_email(env: Env) -> None:
    result = env.invoke("generate", "--name", NAME, "--email", EMAIL)
    assert result.exit_code == 0
    template = env.items()[0]
    key = next(f["value"] for f in template["fields"] if f["label"] == "API key")  # type: ignore[attr-defined]
    assert not _leaks(result.output, key, EMAIL)
    assert "token_id=" in result.output
    assert "expires_at=" in result.output
    assert f"texas-turnout-scraper: {NAME}" in result.output
    assert "scope=aggregates" in result.output


def test_show_prints_the_key_but_still_not_the_email(env: Env) -> None:
    result = env.invoke("generate", "--name", NAME, "--email", EMAIL, "--show")
    template = env.items()[0]
    key = next(f["value"] for f in template["fields"] if f["label"] == "API key")  # type: ignore[attr-defined]
    assert f"key={key}" in result.output
    assert EMAIL not in result.output
    assert hash_token(key) in {
        row[0] for row in env.cf.db.execute("SELECT token_hash FROM consumer_api_token")
    }


def test_key_goes_on_stdin_not_in_argv(env: Env) -> None:
    env.invoke("generate", "--name", NAME, "--email", EMAIL)
    args, stdin = env.op.calls[0]
    key = next(f["value"] for f in env.items()[0]["fields"] if f["label"] == "API key")  # type: ignore[attr-defined]
    assert key in (stdin or "")
    assert args[:3] == ["item", "create", "-"]
    assert all(key not in a and EMAIL not in a and NAME not in a for a in args)
    assert "qxyxd4oqdy5mpmg4t4qh3bfflm" in args


def test_item_shape_and_instructions(env: Env) -> None:
    env.invoke("generate", "--name", NAME, "--email", EMAIL, "--days", "7")
    template = env.items()[0]
    assert template["title"] == f"texas-turnout-scraper: {NAME}"
    assert template["category"] == "API_CREDENTIAL"
    token_id = env.field("Token ID")
    assert template["tags"] == ["texas-turnout-scraper", f"token:{token_id}"]
    labels = [f["label"] for f in template["fields"]]  # type: ignore[attr-defined]
    assert labels == [
        "API key",
        "Endpoint",
        "Scope",
        "Name",
        "Email Address",
        "Expires",
        "Token ID",
        "Instructions",
        "notesPlain",
    ]
    expires = env.field("Expires")
    expected = (dt.datetime.now(tz=dt.timezone.utc) + dt.timedelta(days=7)).date().isoformat()
    assert expires in {
        expected,
        (dt.date.fromisoformat(expected) - dt.timedelta(days=1)).isoformat(),
    }
    text = env.field("Instructions")
    for needle in (
        "TX_TURNOUT_API_KEY",
        "TX_TURNOUT_API_NAME",
        "TX_TURNOUT_API_EMAIL",
        ".env",
        "Authorization: Bearer $TX_TURNOUT_API_KEY",
        "X-API-Key: $TX_TURNOUT_API_KEY",
        "https://ev-api-preview.abstractdata.io",
        "https://ev-api-preview.abstractdata.io/elections",
        "CF-Access-Client-Id",
        "CF-Access-Client-Secret",
        expires,
        "never commit",
        "Never paste",
    ):
        assert needle.lower() in text.lower(), needle
    key = next(f["value"] for f in template["fields"] if f["label"] == "API key")  # type: ignore[attr-defined]
    assert key not in text
    assert env.field("Endpoint") == "https://ev-api-preview.abstractdata.io/elections"
    assert env.field("Scope") == "aggregates"
    assert env.field("notesPlain") == text
    endpoint = next(f for f in template["fields"] if f["label"] == "Endpoint")  # type: ignore[attr-defined]
    assert endpoint["type"] == "URL"


def test_write_order_is_1password_then_d1_then_kv_with_ttl(env: Env) -> None:
    result = env.invoke("generate", "--name", NAME, "--email", EMAIL)
    assert result.exit_code == 0
    assert env.events == ["op:create", "d1:insert", "kv:put"]
    ((_value, params),) = env.cf.kv.values()
    ttl = int(params["expiration_ttl"])
    assert 30 * 86400 - 120 <= ttl <= 30 * 86400
    row = env.cf.db.execute("SELECT label, scope, expires_at FROM consumer_api_token").fetchone()
    assert row["label"] == NAME
    assert row["scope"] == "aggregates"
    assert EMAIL not in json.dumps(dict(row))


def test_roster_scope_is_explicit(env: Env) -> None:
    env.invoke("generate", "--name", NAME, "--email", EMAIL, "--scope", "roster")
    assert env.cf.db.execute("SELECT scope FROM consumer_api_token").fetchone()["scope"] == "roster"
    assert next(iter(env.cf.kv.values()))[0] == "roster"
    assert env.field("Scope") == "roster"
    note = env.field("Instructions")
    assert "Scope roster" in note
    assert "/roster/<YYYY-MM-DD>" in note
    assert env.field("notesPlain") == note


def test_bad_scope_exits_2_before_any_write(env: Env) -> None:
    result = env.invoke("generate", "--name", NAME, "--email", EMAIL, "--scope", "admin")
    assert result.exit_code == 2
    assert env.events == []


def test_d1_failure_archives_the_1password_item_and_exits_1(env: Env) -> None:
    env.cf.fail_d1_insert = True
    result = env.invoke("generate", "--name", NAME, "--email", EMAIL)
    assert result.exit_code == 1
    assert env.events == ["op:create", "op:archive"]
    assert "EarlyVoteSyncError" in result.output
    assert not _leaks(result.output, NAME, EMAIL)
    assert env.cf.kv == {}


def test_kv_failure_archives_the_item_and_revokes_the_row(env: Env) -> None:
    env.cf.fail_kv_put = True
    result = env.invoke("generate", "--name", NAME, "--email", EMAIL)
    assert result.exit_code == 1
    assert env.events[:3] == ["op:create", "d1:insert", "op:archive"]
    assert env.cf.db.execute("SELECT revoked_at FROM consumer_api_token").fetchone()[0]


def test_1password_auth_error_writes_nothing_to_cloudflare(env: Env) -> None:
    env.op.error = signed_out()
    result = env.invoke("generate", "--name", NAME, "--email", EMAIL)
    assert result.exit_code == 1
    assert "op signin" in result.output
    assert env.events == []
    assert env.cf.kv == {}
    assert len(env.op.calls) == 1


def test_subprocess_runner_missing_binary_says_op_signin(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise FileNotFoundError("op")

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(OnePasswordAuthError, match="op signin"):
        SubprocessOnePasswordRunner().run(["item", "list"])


def test_subprocess_runner_auth_stderr_maps_to_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    done = subprocess.CompletedProcess(["op"], 1, "", "[ERROR] account is not signed in")
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: done)
    with pytest.raises(OnePasswordAuthError, match="op signin"):
        SubprocessOnePasswordRunner().run(["item", "list"])


def test_subprocess_runner_other_failure_has_no_stderr_in_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    done = subprocess.CompletedProcess(["op"], 1, "", f"bad item for {EMAIL} {NAME}")
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: done)
    with pytest.raises(OnePasswordError) as caught:
        SubprocessOnePasswordRunner().run(["item", "create", "-"], stdin="{}")
    assert not _leaks(str(caught.value), EMAIL, NAME)


def test_subprocess_runner_passes_stdin_and_a_fixed_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen["argv"], seen["input"] = argv, kwargs["input"]
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    SubprocessOnePasswordRunner().run(["item", "create", "-"], stdin='{"k":"secret-value"}')
    assert seen["argv"] == ["op", "item", "create", "-"]
    assert seen["input"] == '{"k":"secret-value"}'


def test_tokens_command_group_is_gone(env: Env) -> None:
    for sub in ("mint", "list", "revoke"):
        result = CliRunner().invoke(app, ["tokens", sub])
        assert result.exit_code == 2
        assert "No such command" in result.output
    help_text = CliRunner().invoke(app, ["--help"]).output
    assert "tokens" not in help_text
    assert "api" in help_text


def _issue(env: Env, *, days: int = 30) -> str:
    env.invoke("generate", "--name", NAME, "--email", EMAIL, "--days", str(days))
    return env.field("Token ID")


def test_extend_updates_d1_kv_and_1password(env: Env) -> None:
    token_id = _issue(env, days=10)
    env.events.clear()
    result = env.invoke("extend", token_id, "--days", "5")
    assert result.exit_code == 0, result.output
    assert env.events == ["d1:update_expiry", "kv:put", "op:edit"]
    expires = env.cf.db.execute("SELECT expires_at FROM consumer_api_token").fetchone()[0]
    parsed = dt.datetime.fromisoformat(expires)
    delta = parsed - dt.datetime.now(tz=dt.timezone.utc)
    assert dt.timedelta(days=14, hours=23) < delta <= dt.timedelta(days=15)
    ((_value, params),) = env.cf.kv.values()
    assert abs(int(params["expiration_ttl"]) - delta.total_seconds()) < 120
    args, _ = env.op.calls[-1]
    assert f"Expires[date]={expires[:10]}" in args
    instructions = next(a for a in args if a.startswith("Instructions[text]="))
    notes = next(a for a in args if a.startswith("notesPlain[text]="))
    assert expires[:10] in instructions
    assert "/elections" in instructions
    assert notes.removeprefix("notesPlain[text]=") == instructions.removeprefix(
        "Instructions[text]="
    )
    assert "Endpoint[url]=https://ev-api-preview.abstractdata.io/elections" in args
    assert "Scope[text]=aggregates" in args
    assert not _leaks(" ".join(args), EMAIL, NAME)


def test_extend_with_set_expiry_on_a_never_expiring_token_starts_from_now(env: Env) -> None:
    env.cf.db.executescript(
        """CREATE TABLE consumer_api_token (token_id TEXT PRIMARY KEY, token_hash TEXT UNIQUE,
        label TEXT, created_at TEXT, revoked_at TEXT, scope TEXT DEFAULT 'aggregates');
        INSERT INTO consumer_api_token
        VALUES ('old','v2:abc','legacy','2026-01-01T00:00:00+00:00',NULL,'aggregates');"""
    )
    result = env.invoke("extend", "old", "--days", "3", "--set-expiry")
    assert result.exit_code == 0, result.output
    expires = env.cf.db.execute("SELECT expires_at FROM consumer_api_token").fetchone()[0]
    delta = dt.datetime.fromisoformat(expires) - dt.datetime.now(tz=dt.timezone.utc)
    assert dt.timedelta(days=2, hours=23) < delta <= dt.timedelta(days=3)
    assert "no 1Password item" in result.output


def test_extend_without_a_1password_item_still_succeeds_with_a_warning(env: Env) -> None:
    token_id = _issue(env)
    env.op.items.clear()
    result = env.invoke("extend", token_id)
    assert result.exit_code == 0
    assert "Warning" in result.output
    assert env.events[-2:] == ["d1:update_expiry", "kv:put"]


def test_extend_refuses_a_revoked_token(env: Env) -> None:
    token_id = _issue(env)
    env.invoke("revoke", token_id)
    env.events.clear()
    result = env.invoke("extend", token_id)
    assert result.exit_code == 1
    assert "revoked" in result.output
    assert env.events == []


def test_extend_unknown_token_exits_1(env: Env) -> None:
    result = env.invoke("extend", "nope")
    assert result.exit_code == 1


def test_list_shows_status_expiry_and_never_the_email(env: Env) -> None:
    active = _issue(env)
    env.cf.db.execute(
        "INSERT INTO consumer_api_token VALUES ('exp','h2','old-label','2026-01-01T00:00:00+00:00',"
        "NULL,'aggregates','2026-02-01T00:00:00+00:00')"
    )
    env.cf.db.execute(
        "INSERT INTO consumer_api_token VALUES ('rev','h3','r','2026-01-01T00:00:00+00:00',"
        "'2026-01-02T00:00:00+00:00','roster','2099-01-01T00:00:00+00:00')"
    )
    env.cf.db.execute(
        "INSERT INTO consumer_api_token VALUES ('nev','h4','legacy','2026-01-01T00:00:00+00:00',"
        "NULL,'aggregates',NULL)"
    )
    env.cf.db.commit()
    result = env.invoke("list")
    assert result.exit_code == 0
    rows = {line.split("\t")[0]: line.split("\t") for line in result.output.splitlines()}
    assert rows[active][1] == "active"
    assert rows["exp"][1] == "expired"
    assert rows["rev"][1] == "revoked"
    assert rows["nev"][1] == "active" and rows["nev"][4] == "never"
    assert EMAIL not in result.output


def test_revoke_keeps_semantics_and_leaves_1password_alone(env: Env) -> None:
    token_id = _issue(env)
    calls_before = len(env.op.calls)
    result = env.invoke("revoke", token_id)
    assert result.exit_code == 0
    assert "kv:delete" in env.events
    assert len(env.op.calls) == calls_before
    assert env.invoke("revoke", "missing").exit_code == 1


def test_days_out_of_range_exits_2(env: Env) -> None:
    for bad in ("0", "-1", "99999"):
        assert (
            env.invoke("generate", "--name", NAME, "--email", EMAIL, "--days", bad).exit_code == 2
        )
    assert env.events == []


def test_instructions_show_the_exact_env_lines_to_paste(env: Env) -> None:
    env.invoke("generate", "--name", NAME, "--email", EMAIL)
    text = env.field("Instructions")
    assert 'TX_TURNOUT_API_KEY=<the "API key" field>' in text
    assert 'TX_TURNOUT_API_NAME=<the "Name" field>' in text
    assert 'TX_TURNOUT_API_EMAIL=<the "Email Address" field>' in text
    assert 'TX_TURNOUT_API_ENDPOINT=<the "Endpoint" field>' in text
    assert "GET https://ev-api-preview.abstractdata.io/elections" in text


def test_a_permission_error_is_not_reported_as_a_signin_problem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr = "[ERROR] you do not have authorization to create items in this vault"
    done = subprocess.CompletedProcess(["op"], 1, "", stderr)
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: done)
    with pytest.raises(OnePasswordError) as raised:
        SubprocessOnePasswordRunner().run(["item", "create"])
    assert not isinstance(raised.value, OnePasswordAuthError)
    assert "signin" not in str(raised.value)


def test_base_url_and_vault_can_be_overridden_from_the_environment(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TX_TURNOUT_API_BASE_URL", "https://api.example.test")
    monkeypatch.setenv("TX_TURNOUT_OP_VAULT", "othervault")
    store = api_cli.OnePasswordKeyStore(env.op)
    store.create(name=NAME, email=EMAIL, api_key="k", token_id="t1", expires_date="2026-12-01")
    args, stdin = env.op.calls[0]
    assert args[args.index("--vault") + 1] == "othervault"
    instructions = next(
        f["value"] for f in json.loads(stdin or "{}")["fields"] if f["label"] == "Instructions"
    )
    hosts = {
        (parsed.scheme, parsed.hostname)
        for parsed in (urlparse(url) for url in re.findall(r"https?://[^\s]+", instructions))
    }
    assert ("https", "api.example.test") in hosts


def _legacy_never_expires(env: Env) -> str:
    env.invoke("generate", "--name", NAME, "--email", EMAIL)
    env.cf.db.execute("UPDATE consumer_api_token SET expires_at = NULL")
    env.cf.db.commit()
    return str(env.cf.db.execute("SELECT token_id FROM consumer_api_token").fetchone()[0])


def test_extend_refuses_a_key_that_never_expires_without_set_expiry(env: Env) -> None:
    token_id = _legacy_never_expires(env)
    result = env.invoke("extend", token_id, "--days", "10")
    assert result.exit_code == 1
    assert "--set-expiry" in result.output
    assert env.cf.db.execute("SELECT expires_at FROM consumer_api_token").fetchone()[0] is None


def test_extend_with_set_expiry_puts_an_expiry_on_a_never_expiring_key(env: Env) -> None:
    token_id = _legacy_never_expires(env)
    result = env.invoke("extend", token_id, "--days", "10", "--set-expiry")
    assert result.exit_code == 0
    assert env.cf.db.execute("SELECT expires_at FROM consumer_api_token").fetchone()[0]


def test_ctrl_c_between_the_1password_and_cloudflare_writes_rolls_back(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(*_a: object, **_k: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(api_cli.ConsumerTokenAdmin, "store", interrupted)
    result = env.invoke("generate", "--name", NAME, "--email", EMAIL)
    assert result.exit_code != 0
    assert env.events == ["op:create", "op:archive"]


def test_failed_cleanup_warning_names_the_token_id(env: Env) -> None:
    env.cf.fail_d1_insert = True
    env.op.error = OnePasswordError("op exited with status 1")
    env.op.fail_on = lambda args: args[1] == "delete"
    result = env.invoke("generate", "--name", NAME, "--email", EMAIL)
    assert result.exit_code == 1
    token_id = next(line for line in result.output.splitlines() if "token_id" in line)
    assert token_id.split("=")[-1].strip()


def test_extend_1password_failure_prints_the_target_date(env: Env) -> None:
    env.invoke("generate", "--name", NAME, "--email", EMAIL)
    token_id = str(env.cf.db.execute("SELECT token_id FROM consumer_api_token").fetchone()[0])
    env.op.error = OnePasswordError("op exited with status 1")
    env.op.fail_on = lambda args: args[1] == "edit"
    result = env.invoke("extend", token_id, "--days", "5")
    assert result.exit_code == 1
    expires = str(env.cf.db.execute("SELECT expires_at FROM consumer_api_token").fetchone()[0])
    assert expires[:10] in result.output
    assert "do not run extend again" in result.output.lower()


def test_schema_is_checked_once_per_admin(env: Env) -> None:
    admin = env.cf.admin()
    admin.list_tokens()
    admin.list_tokens()
    admin.close()
    assert sum("pragma_table_info" in sql for sql in env.cf.sql) == 1
