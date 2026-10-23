"""Size caps for untrusted remote payloads."""

from __future__ import annotations

import io
import zipfile
from types import SimpleNamespace

import pytest

from texas_turnout_scraper import http_transport
from texas_turnout_scraper.civix import envelope, evr
from texas_turnout_scraper.http_transport import PayloadTooLargeError, check_response_size


def test_decode_envelope_rejects_oversized_upload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(envelope, "MAX_DECODED_PAYLOAD_BYTES", 8)
    resp = SimpleNamespace(text="x", json=lambda: {"upload": "A" * 100})
    with pytest.raises(PayloadTooLargeError):
        envelope._decode_envelope(resp)


def test_decode_envelope_accepts_small_upload() -> None:
    resp = SimpleNamespace(text="x", json=lambda: {"upload": "aGVsbG8="})
    assert envelope._decode_envelope(resp) == b"hello"


def test_check_response_size_by_header_and_body() -> None:
    with pytest.raises(PayloadTooLargeError):
        check_response_size(SimpleNamespace(headers={"content-length": "100"}, content=b""), 10)
    with pytest.raises(PayloadTooLargeError):
        check_response_size(SimpleNamespace(headers={}, content=b"x" * 11), 10)
    check_response_size(SimpleNamespace(headers={}, content=b"x" * 10), 10)
    assert http_transport.MAX_RESPONSE_BYTES > http_transport.MAX_DECODED_PAYLOAD_BYTES


def _zip_with(names: list[str], payload: bytes = b"a,b\n") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in names:
            zf.writestr(name, payload)
    return buf.getvalue()


def test_zip_member_count_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(evr, "_ZIP_MAX_MEMBERS", 2)
    with zipfile.ZipFile(io.BytesIO(_zip_with(["a.csv", "b.csv", "c.csv"]))) as zf:
        with pytest.raises(PayloadTooLargeError):
            evr._check_zip_limits(zf)


def test_zip_size_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    data = _zip_with(["a.csv", "b.csv"], b"x" * 10)
    monkeypatch.setattr(evr, "_ZIP_MAX_MEMBER_BYTES", 5)
    with zipfile.ZipFile(io.BytesIO(data)) as zf, pytest.raises(PayloadTooLargeError):
        evr._check_zip_limits(zf)
    monkeypatch.setattr(evr, "_ZIP_MAX_MEMBER_BYTES", 100)
    monkeypatch.setattr(evr, "_ZIP_MAX_TOTAL_BYTES", 15)
    with zipfile.ZipFile(io.BytesIO(data)) as zf, pytest.raises(PayloadTooLargeError):
        evr._check_zip_limits(zf)


def test_parse_statewide_zip_enforces_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(evr, "_ZIP_MAX_MEMBERS", 0)
    from datetime import date

    with pytest.raises(PayloadTooLargeError):
        evr.parse_ed_statewide_voter_records(
            _zip_with(["STATEWIDE_VOTER_INFO.csv"]), election_id="1", report_date=date(2024, 1, 1)
        )
