"""Shared read contracts for scope review and move-job summaries."""

from enum import StrEnum
from typing import Annotated, Self

from pydantic import ConfigDict, Field, model_validator

from app.contracts.model import ContractModel

MAX_AMOUNT_KRW = 100_000_000_000


class QuoteContractModel(ContractModel):
    """Accept JSON primitives while retaining the repository's strict response shape."""

    model_config = ConfigDict(strict=False)


class QuoteAdjustment(QuoteContractModel):
    label: Annotated[str, Field(min_length=1, max_length=200)]
    amount_krw: Annotated[int, Field(ge=-MAX_AMOUNT_KRW, le=MAX_AMOUNT_KRW)]


class QuoteSnapshot(QuoteContractModel):
    base_amount_krw: Annotated[int, Field(ge=0, le=MAX_AMOUNT_KRW)]
    adjustments: Annotated[tuple[QuoteAdjustment, ...], Field(max_length=100)] = ()
    total_amount_krw: Annotated[int, Field(ge=0, le=MAX_AMOUNT_KRW)]

    @model_validator(mode="after")
    def require_exact_total_and_unique_labels(self) -> Self:
        labels = [adjustment.label for adjustment in self.adjustments]
        if len(labels) != len(set(labels)):
            raise ValueError("quote adjustment labels must be unique")
        if (
            self.base_amount_krw + sum(adjustment.amount_krw for adjustment in self.adjustments)
            != self.total_amount_krw
        ):
            raise ValueError("quote total must equal base plus adjustments")
        return self


class ScopeReviewStatus(StrEnum):
    COMPANY_REVIEW = "company_review"
    CUSTOMER_REVIEW = "customer_review"
    REVISION_REQUESTED = "revision_requested"
    CONFIRMED = "confirmed"


class CompanyParticipationStatus(StrEnum):
    NOT_INVITED = "company_not_invited"
    INVITED = "company_invited"
    JOINED = "company_joined"
    DECLINED = "company_declined"
    EXPIRED = "company_invitation_expired"
    REVOKED = "company_invitation_revoked"
