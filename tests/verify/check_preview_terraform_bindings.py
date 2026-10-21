"""Preview Worker Terraform must wire Access, shell DB, election D1, and EV_D1_BINDINGS."""

from __future__ import annotations

import re

from tests.verify.preview_tf_bindings import (
    preview_election_d1_binding_name,
    preview_tf_text,
    worker_d1_binding_names,
)


def test_preview_worker_version_binds_access_and_d1_map() -> None:
    text = preview_tf_text()
    assert 'name = "ACCESS_CLIENT_ID"' in text
    assert 'name = "ACCESS_CLIENT_SECRET"' in text
    assert 'type = "secret_text"' in text
    assert 'name = "EV_D1_BINDINGS"' in text
    assert "local.ev_d1_bindings" in text
    assert "local.preview_election_d1_binding" in text
    assert "cloudflare_zero_trust_access_service_token.preview.client_id" in text
    assert "cloudflare_zero_trust_access_service_token.preview.client_secret" in text
    assert "cloudflare_workers_custom_domain.preview" in text
    assert "preview_api_hostname" in text
    assert re.search(r'name\s*=\s*"DB"', text) is not None
    assert "cloudflare_d1_database.election_preview.id" in text


def test_ev_d1_bindings_match_worker_d1_bindings() -> None:
    text = preview_tf_text()
    election_binding = preview_election_d1_binding_name()
    d1_names = worker_d1_binding_names()
    assert "DB" in d1_names, "shell consumer-token D1 binding DB is required"
    assert election_binding in d1_names, (
        f"election binding {election_binding!r} must appear in worker d1 bindings"
    )

    assert "(var.preview_source_election_id) = local.preview_election_d1_binding" in text
    map_values = {election_binding}
    extra_election_d1 = [name for name in d1_names if name != "DB"]
    assert set(extra_election_d1) == map_values, (
        f"worker d1 bindings {extra_election_d1!r} must match EV_D1_BINDINGS targets {map_values!r}"
    )

    assert "import {" in text and "cloudflare_d1_database.election_preview" in text
