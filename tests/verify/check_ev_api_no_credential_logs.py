"""Guardrail: preview Worker ``ev_api`` must not log credentials or request headers.

Invocation logs are disabled in Terraform; this check catches regressions in
application logging (``logger`` / ``print``) under ``infra/preview/worker/src/ev_api/``.

Run via: ``uv run pytest tests/verify/check_ev_api_no_credential_logs.py -v``
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EV_API_DIR = REPO_ROOT / "infra" / "preview" / "worker" / "src" / "ev_api"

_FORBIDDEN_LOG_SUBSTRINGS = (
    "cf-access-client-secret",
    "cf-access-client-id",
    "authorization",
    "x-api-key",
    "access_client_secret",
    "client_secret",
    "bearer",
    "consumer_token",
    "x_api_key",
)

_FORBIDDEN_LOG_NAMES = frozenset(
    {
        "headers",
        "authorization",
        "credential",
        "api_token",
        "access_client_secret",
        "client_secret",
        "raw_token",
        "digest",
    }
)

_LOGGER_METHODS = frozenset({"debug", "info", "warning", "error", "exception", "critical"})


def _py_files() -> list[Path]:
    if not EV_API_DIR.is_dir():
        msg = f"missing ev_api tree: {EV_API_DIR}"
        raise AssertionError(msg)
    return sorted(EV_API_DIR.glob("*.py"))


def _string_fragments(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
        return parts
    return []


def _collect_log_call_issues(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    issues: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        is_logger = (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "logger"
            and node.func.attr in _LOGGER_METHODS
        )
        is_print = isinstance(node.func, ast.Name) and node.func.id == "print"
        if not is_logger and not is_print:
            continue
        if is_logger and node.func.attr == "exception":
            for kw in node.keywords:
                if kw.arg == "exc_info" and isinstance(kw.value, ast.Constant):
                    if kw.value.value is True:
                        issues.append(f"{path}:{node.lineno}: logger.exception(exc_info=True)")
        for arg in list(node.args) + [kw.value for kw in node.keywords if kw.arg != "exc_info"]:
            for text in _string_fragments(arg):
                lowered = text.lower()
                for needle in _FORBIDDEN_LOG_SUBSTRINGS:
                    if needle in lowered:
                        issues.append(
                            f"{path}:{node.lineno}: log literal contains forbidden substring"
                        )
            if isinstance(arg, ast.Name) and arg.id in _FORBIDDEN_LOG_NAMES:
                issues.append(f"{path}:{node.lineno}: logs sensitive name {arg.id!r}")
            if isinstance(arg, ast.Attribute) and arg.attr in _FORBIDDEN_LOG_NAMES:
                issues.append(f"{path}:{node.lineno}: logs sensitive attribute {arg.attr!r}")
        for kw in node.keywords:
            if (
                kw.arg == "exc_info"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True
            ):
                issues.append(f"{path}:{node.lineno}: log call uses exc_info=True")
    return issues


def _check_unhandled_exception_handler(path: Path) -> list[str]:
    if path.name != "app.py":
        return []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    issues: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef) or node.name != "unhandled_exception":
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            if not (
                isinstance(child.func, ast.Attribute)
                and isinstance(child.func.value, ast.Name)
                and child.func.value.id == "logger"
                and child.func.attr == "error"
            ):
                continue
            if len(child.args) != 3:
                issues.append(
                    f"{path}:{child.lineno}: unhandled_exception logger.error must use "
                    "type name + route template only"
                )
    return issues


def check_ev_api_no_credential_logs() -> None:
    all_issues: list[str] = []
    for path in _py_files():
        all_issues.extend(_collect_log_call_issues(path))
        all_issues.extend(_check_unhandled_exception_handler(path))
    if all_issues:
        sample = "\n".join(f"  - {item}" for item in all_issues[:20])
        extra = ""
        if len(all_issues) > 20:
            extra = f"\n  ... and {len(all_issues) - 20} more"
        msg = "ev_api credential logging guard failed:\n" + sample + extra
        raise AssertionError(msg)


def test_ev_api_no_credential_logs() -> None:
    check_ev_api_no_credential_logs()


if __name__ == "__main__":
    check_ev_api_no_credential_logs()
    print("check_ev_api_no_credential_logs: OK")
