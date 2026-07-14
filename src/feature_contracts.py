"""Shared contracts for daily consulting and Instagram publishing APIs."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolExecutionResponse(StrictModel):
    toolName: str
    status: Literal["SUCCESS", "FAILED", "SKIPPED"]
    arguments: dict[str, Any] | None = None
    resultSummary: str | None = None
    latencyMs: int | None = Field(default=None, ge=0)


class CitationResponse(StrictModel):
    title: str
    url: str
    organization: str | None = None


class ConsultingCause(StrictModel):
    title: str
    description: str
    confidence: float = Field(ge=0, le=1)
    evidence: list[str]


class ConsultingRecommendation(StrictModel):
    priority: Literal["HIGH", "MEDIUM", "LOW"]
    title: str
    description: str
    expectedEffect: str | None = None


class ConsultingMetric(StrictModel):
    metricName: str
    currentValue: float
    previousValue: float | None = None
    changeRate: float | None = None
    unit: str


class GenerateDailyConsultingRequest(StrictModel):
    userId: int = Field(gt=0)
    storeId: int = Field(gt=0)
    targetDate: date
    reportFormat: Literal["DAILY_V1"] = "DAILY_V1"


class GenerateDailyConsultingResponse(StrictModel):
    title: str = Field(min_length=1)
    targetDate: date
    summary: str = Field(min_length=1)
    content: str = Field(min_length=1)
    chatInsights: list[str]
    keyMetrics: list[ConsultingMetric]
    externalFactors: list[str]
    estimatedCauses: list[ConsultingCause]
    recommendations: list[ConsultingRecommendation]
    warnings: list[str]
    usedTools: list[ToolExecutionResponse]
    citations: list[CitationResponse]
    model: str = Field(min_length=1)


class PublishInstagramRequest(StrictModel):
    userId: int = Field(gt=0)
    instagramAccountId: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=2200)
    hashtags: list[str] = Field(default_factory=list, max_length=30)
    imageUrls: list[str] = Field(min_length=1, max_length=10)
    publishType: Literal["FEED", "CAROUSEL"]
    idempotencyKey: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_instagram_shape(self) -> "PublishInstagramRequest":
        if any(not url.startswith("https://") for url in self.imageUrls):
            raise ValueError("imageUrls must use HTTPS")
        if self.publishType == "FEED" and len(self.imageUrls) != 1:
            raise ValueError("FEED requires exactly one image")
        if self.publishType == "CAROUSEL" and len(self.imageUrls) < 2:
            raise ValueError("CAROUSEL requires at least two images")
        return self


class PublishInstagramResponse(StrictModel):
    marketingId: int = Field(gt=0)
    platform: Literal["INSTAGRAM"] = "INSTAGRAM"
    status: Literal["PUBLISHED", "FAILED", "PROCESSING"]
    externalPostId: str | None = None
    publishedUrl: str | None = None
    publishedAt: datetime | None = None
    failureReason: str | None = None
