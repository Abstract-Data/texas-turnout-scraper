"""Pydantic models for Karl Rove report aggregates (no VUID fields)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class VepYearCount(BaseModel):
    model_config = ConfigDict(frozen=True)

    vep_cohort_year: int = Field(description="VEP cohort year")
    registrants: int = Field(ge=0)


class VepYearsSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    years: tuple[VepYearCount, ...]
    total: int = Field(ge=0)


class CohortEvRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    vep_cohort_year: int
    registrants: int = Field(ge=0)
    early_voted: int = Field(ge=0)
    percent_early_voted: float = Field(ge=0.0, le=100.0)


class RegistrationYearEvRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: str
    registration_year: int | None = None
    registrants: int = Field(ge=0)
    early_voted: int = Field(ge=0)
    percent_early_voted: float = Field(ge=0.0, le=100.0)
    future_year_data_quality: bool = False


class KarlRoveReportData(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_election_id: str
    election_label: str
    as_of_ev_date: str
    generated_at_iso: str
    vep_registrations: VepYearsSummary
    cohort_early_vote: tuple[CohortEvRow, ...]
    cohort_early_vote_total: CohortEvRow
    registration_year_early_vote: tuple[RegistrationYearEvRow, ...]
    registration_year_early_vote_total: RegistrationYearEvRow
    is_fixture: bool = False
