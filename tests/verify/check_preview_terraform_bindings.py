"""Preview Worker Terraform must wire Access and D1 map env bindings."""

from __future__ import annotations

from pathlib import Path

MAIN_TF = Path(__file__).resolve().parents[2] / "infra" / "preview" / "main.tf"


def test_preview_worker_version_binds_access_and_d1_map() -> None:
    text = MAIN_TF.read_text(encoding="utf-8")
    assert 'name = "ACCESS_CLIENT_ID"' in text
    assert 'name = "ACCESS_CLIENT_SECRET"' in text
    assert 'type = "secret_text"' in text
    assert 'name = "EV_D1_BINDINGS"' in text
    assert "local.ev_d1_bindings" in text
    assert "cloudflare_zero_trust_access_service_token.preview.client_id" in text
    assert "cloudflare_zero_trust_access_service_token.preview.client_secret" in text
