"""Shared contracts for daily consulting and Instagram publishing APIs."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


KST = ZoneInfo("Asia/Seoul")


def _backend_local_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(KST).replace(tzinfo=None)


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
    userId: int | None = Field(default=None)
    storeId: int | None = Field(default=None)
    targetDate: date | None = None
    reportFormat: str | None = None

    @model_validator(mode="after")
    def normalize_fields(self) -> "GenerateDailyConsultingRequest":
        if self.userId is None:
            self.userId = 0
        if self.storeId is None:
            self.storeId = 0
        if self.targetDate is None:
            self.targetDate = date.today()
        if self.reportFormat is None:
            self.reportFormat = "DAILY_V1"
        return self


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


class SaleTransactionInput(StrictModel):
    soldAt: datetime
    totalPaidAmount: int = Field(ge=0)
    status: Literal["COMPLETED", "CANCELLED"]


class ClosingSalesForecastRequest(StrictModel):
    storeId: int | None = Field(default=None)
    asOf: datetime | None = None
    salesData: list[SaleTransactionInput] = Field(default_factory=list, max_length=5000)

    @model_validator(mode="after")
    def normalize_fields(self) -> "ClosingSalesForecastRequest":
        if self.storeId is None:
            self.storeId = 0
        if self.asOf is None:
            self.asOf = datetime.now()
        return self


class ClosingSalesForecastResponse(StrictModel):
    storeId: int = Field(gt=0)
    businessDate: date
    currency: Literal["KRW"] = "KRW"
    observedSalesAmount: int = Field(ge=0)
    forecastClosingSalesAmount: int = Field(ge=0)
    model: str = Field(min_length=1)
    sampleDays: int = Field(ge=0)
    generatedAt: datetime

    _normalize_generated_at = field_validator("generatedAt", mode="after")(
        _backend_local_datetime
    )


class TomorrowVisitorsForecastRequest(StrictModel):
    storeId: int | None = Field(default=None)
    baseDate: date | None = None
    salesData: list[SaleTransactionInput] = Field(default_factory=list, max_length=5000)

    @model_validator(mode="after")
    def normalize_fields(self) -> "TomorrowVisitorsForecastRequest":
        if self.storeId is None:
            self.storeId = 0
        if self.baseDate is None:
            self.baseDate = date.today()
        return self


class TomorrowVisitorsForecastResponse(StrictModel):
    storeId: int = Field(gt=0)
    targetDate: date
    expectedVisitors: int = Field(ge=0)
    model: str = Field(min_length=1)
    sampleDays: int = Field(ge=0)
    generatedAt: datetime

    _normalize_generated_at = field_validator("generatedAt", mode="after")(
        _backend_local_datetime
    )


class GenerateMarketingCopyRequest(StrictModel):
    userId: int = Field(gt=0)
    imageUrls: list[str] = Field(min_length=1, max_length=10)
    draftText: str = Field(min_length=1, max_length=2000)
    tags: list[str] = Field(default_factory=list, max_length=30)
    tone: str | None = Field(default=None, max_length=100)
    additionalRequest: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_image_urls(self) -> "GenerateMarketingCopyRequest":
        if any(not url.startswith(("http://", "https://")) for url in self.imageUrls):
            raise ValueError("imageUrls must use HTTP or HTTPS")
        return self


class GenerateMarketingCopyResponse(StrictModel):
    content: str = Field(min_length=1, max_length=2200)
    model: str = Field(min_length=1)


class PublishInstagramRequest(StrictModel):
    userId: int = Field(gt=0)
    instagramUsername: str = Field(min_length=1, max_length=100)
    instagramPassword: SecretStr = Field(min_length=8, max_length=200)
    content: str = Field(min_length=1, max_length=2200)
    hashtags: list[str] = Field(default_factory=list, max_length=30)
    images: list[dict[str, Any]] = Field(min_length=1, max_length=10)
    idempotencyKey: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_instagram_shape(self) -> "PublishInstagramRequest":
        for image in self.images:
            if not isinstance(image, dict):
                raise ValueError("each image entry must be an object")
            filename = image.get("filename")
            content = image.get("content")
            content_type = image.get("contentType")
            if not isinstance(filename, str) or not filename.strip():
                raise ValueError("image filename is required")
            if not isinstance(content_type, str) or not content_type.strip():
                raise ValueError("image contentType is required")
            if not isinstance(content, str) or not content:
                raise ValueError("image content is required")
        return self


class PublishInstagramResponse(StrictModel):
    marketingId: int = Field(gt=0)
    platform: Literal["INSTAGRAM"] = "INSTAGRAM"
    status: Literal["PUBLISHED", "FAILED", "PROCESSING"]
    externalPostId: str | None = None
    publishedUrl: str | None = None
    publishedAt: datetime | None = None
    failureReason: str | None = None

    _normalize_published_at = field_validator("publishedAt", mode="after")(
        lambda value: _backend_local_datetime(value) if value is not None else None
    )
