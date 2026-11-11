"""Pydantic models: raw source postings, LLM extraction output, canonical records."""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class Seniority(str, Enum):
    junior = "junior"
    mid = "mid"
    senior = "senior"
    staff_plus = "staff_plus"


class RemotePolicy(str, Enum):
    onsite = "onsite"
    hybrid = "hybrid"
    remote = "remote"


class EmploymentType(str, Enum):
    full_time = "full_time"
    contract = "contract"
    intern = "intern"


class Status(str, Enum):
    open = "open"
    closed = "closed"


class SalaryPeriod(str, Enum):
    year = "year"
    month = "month"
    week = "week"
    day = "day"
    hour = "hour"


class RawPosting(BaseModel):
    """A posting as parsed from one source, before LLM extraction. One HN comment may yield one
    RawPosting containing several roles; the extractor splits it."""

    source: str  # greenhouse | lever | hackernews
    board: str  # greenhouse token, lever site, or HN thread id
    source_job_id: str
    url: str
    company_hint: Optional[str] = None  # company name known from the source (ATS boards)
    title_hint: Optional[str] = None
    location_hint: Optional[str] = None
    text: str  # cleaned plain-text description (HTML stripped)
    posted_date_hint: Optional[date] = None
    structured: dict = Field(default_factory=dict)  # structured ATS fields (e.g. Lever salaryRange)
    outbound_urls: list[str] = Field(default_factory=list)  # links inside an HN comment (for dedup)


class ExtractedRole(BaseModel):
    """What the LLM must return for each role. Every non-null field needs an evidence snippet that
    appears verbatim in the input; the extractor nulls fields whose evidence cannot be found."""

    title: str
    company: Optional[str] = None
    seniority: Optional[Seniority] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    remote_policy: Optional[RemotePolicy] = None
    salary_min: Optional[float] = Field(default=None, ge=0)
    salary_max: Optional[float] = Field(default=None, ge=0)
    currency: Optional[str] = None
    salary_period: Optional[SalaryPeriod] = None
    employment_type: Optional[EmploymentType] = None
    skills: list[str] = Field(default_factory=list)
    visa_sponsorship: Optional[bool] = None
    posted_date: Optional[date] = None
    evidence: dict[str, str] = Field(default_factory=dict)

    @field_validator("currency")
    @classmethod
    def _upper(cls, v):
        if v is None:
            return v
        v = v.strip().upper()
        return v if len(v) == 3 and v.isalpha() else None

    @field_validator("skills")
    @classmethod
    def _dedupe_skills(cls, v):
        seen, out = set(), []
        for s in v:
            k = s.strip().lower()
            if k and k not in seen:
                seen.add(k)
                out.append(s.strip())
        return out

    @model_validator(mode="after")
    def _salary_order(self):
        if self.salary_min is not None and self.salary_max is not None and self.salary_min > self.salary_max:
            self.salary_min, self.salary_max = self.salary_max, self.salary_min
        return self


class ExtractionResult(BaseModel):
    roles: list[ExtractedRole]


class CanonicalPosting(BaseModel):
    """The target schema from the assignment."""

    posting_id: str
    company: str
    title: str
    seniority: Optional[Seniority] = None
    city: Optional[str] = None
    country: Optional[str] = None
    remote_policy: Optional[RemotePolicy] = None
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    currency: Optional[str] = None
    employment_type: Optional[EmploymentType] = None
    skills: list[str] = Field(default_factory=list)
    visa_sponsorship: Optional[bool] = None
    posted_date: Optional[date] = None
    status: Status = Status.open
    source_urls: list[str] = Field(default_factory=list)
    evidence: dict[str, str] = Field(default_factory=dict)
    first_seen: datetime
    last_seen: datetime
