"""Pydantic payloads for the Civix ENR and archive clients.

Election ids are strings. A ``field`` validator accepts a JSON number at the
boundary and stores ``str`` of that number. See :func:`require_election_id`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic.functional_validators import BeforeValidator

from .ids import require_election_id

ElectionId = Annotated[str, BeforeValidator(require_election_id)]


class CivixElectionRef(BaseModel):
    """Election identifier discovered from electionConstants."""

    model_config = ConfigDict(frozen=True)

    election_id: ElectionId
    name: str
    year: str
    election_type_code: str
    election_class_code: str


class CivixElectionPayload(BaseModel):
    """Decoded payload for ``/s3/enr/election/{id}``."""

    model_config = ConfigDict(frozen=True)

    election_id: ElectionId
    fetched_at: datetime
    source_updated_at: datetime | None
    source_updated_raw: str
    raw_payload: dict[str, Any]
    decoded_sections: dict[str, Any]


class CivixCountyInfoPayload(BaseModel):
    """Decoded payload for ``/s3/enr/election/countyInfo/{id}``."""

    model_config = ConfigDict(frozen=True)

    election_id: ElectionId
    fetched_at: datetime
    raw_payload: dict[str, Any]
    counties_by_id: dict[str, Any]


class CivixArchiveElectionVersion(BaseModel):
    """Per-election ``Version.json`` payload."""

    model_config = ConfigDict(frozen=True)

    election_id: ElectionId
    version_no: int = Field(gt=0)
    elec_date: str


class CivixArchiveElectionBundle(BaseModel):
    """Fetched archive payloads needed to map one election."""

    model_config = ConfigDict(frozen=True)

    election_ref: CivixElectionRef
    version: CivixArchiveElectionVersion
    office_summary: dict[str, Any]
    county_detail: dict[str, Any] | None = None


# Names used by the package contract. QR calls the same objects
# CivixElectionPayload / CivixCountyInfoPayload.
ElectionPayload = CivixElectionPayload
CountyInfoPayload = CivixCountyInfoPayload
