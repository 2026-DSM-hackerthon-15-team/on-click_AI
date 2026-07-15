from __future__ import annotations

import base64
import ipaddress
import json
import logging
import socket
import time
from datetime import date, datetime, timezone
from typing import Any, Literal
from urllib.parse import urljoin, urlparse

import requests
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from src.auth import require_backend_jwt
from src.consulting import analyze_consulting
from src.daily_consulting import (
    generate_daily_consulting as build_daily_consulting,
    render_daily_content,
)
from src.errors import api_error, install_error_handlers
from src.feature_contracts import (
    ClosingSalesForecastRequest,
    ClosingSalesForecastResponse,
    GenerateMarketingCopyRequest,
    GenerateMarketingCopyResponse,
    GenerateDailyConsultingRequest,
    GenerateDailyConsultingResponse,
    PublishInstagramRequest,
    PublishInstagramResponse,
    TomorrowVisitorsForecastRequest,
    TomorrowVisitorsForecastResponse,
)
from src.instagram import (
    BrowserMCPUnavailable,
    InstagramCredentialsInvalid,
    InstagramLoginChallengeRequired,
    InstagramProviderTimeout,
    publish_instagram as publish_instagram_post,
)
from src.langchain_tools import TOOL_DESCRIPTIONS, get_langchain_tools, get_tool_map
from src.observability import (
    install_browser_log_api,
    install_observability,
    log_event,
    request_headers,
    safe_upstream_target,
)
from src.settings import settings
from src.stats_service.forecasting import predict_closing_sales, predict_tomorrow_visitors


logger = logging.getLogger(__name__)
app = FastAPI(title="ai-service")
install_observability(app, "ai-service")
install_error_handlers(app)


class AiChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    userId: int = Field(gt=0)
    storeId: int = Field(gt=0)
    chatRoomId: int = Field(gt=0)
    message: str = Field(min_length=1)
    availableTools: list[str]
    attachmentKeys: list[str] = Field(default_factory=list)


class AiToolExecutionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    toolName: str
    status: Literal["SUCCESS", "FAILED", "SKIPPED"]
    arguments: dict[str, Any] | None = None
    resultSummary: str | None = None
    latencyMs: int | None = None


class AiCitationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    url: str
    organization: str | None = None


class AiChatResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str = Field(min_length=1)
    usedTools: list[AiToolExecutionResponse]
    citations: list[AiCitationResponse] = Field(default_factory=list)
    model: str = Field(min_length=1)
    finishReason: Literal["STOP", "TOOL_ERROR", "MAX_TOKENS", "SAFETY"] = "STOP"


class SalesDataPoint(BaseModel):
    date: date
    hour: int | None = Field(default=None, ge=0, le=23)
    salesAmount: int = Field(ge=0)
    orderCount: int = Field(ge=0)
    averageOrderValue: float | None = Field(default=None, ge=0)


class CostDataPoint(BaseModel):
    date: date
    category: str
    amount: int = Field(ge=0)


class ExternalContext(BaseModel):
    weatherSummary: str | None = None
    localEvents: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class GenerateConsultingRequest(BaseModel):
    userId: int = Field(gt=0)
    storeId: int = Field(gt=0)
    periodType: Literal["MONTHLY", "QUARTERLY", "YEARLY", "CUSTOM"]
    periodStart: date
    periodEnd: date
    comparisonPeriodStart: date | None = None
    comparisonPeriodEnd: date | None = None
    salesData: list[SalesDataPoint] = Field(min_length=1)
    costData: list[CostDataPoint] = Field(default_factory=list)
    externalContext: ExternalContext | None = None


class ConsultingCauseResponse(BaseModel):
    title: str
    description: str
    confidence: float = Field(ge=0, le=1)
    evidence: list[str]


class ConsultingRecommendationResponse(BaseModel):
    priority: Literal["HIGH", "MEDIUM", "LOW"]
    title: str
    description: str
    expectedEffect: str | None = None


class ConsultingMetricResponse(BaseModel):
    metricName: str
    currentValue: float
    previousValue: float | None = None
    changeRate: float | None = None
    unit: str


class GenerateConsultingResponse(BaseModel):
    title: str
    periodType: str
    periodStart: date
    periodEnd: date
    summary: str
    estimatedCauses: list[ConsultingCauseResponse]
    recommendations: list[ConsultingRecommendationResponse]
    keyMetrics: list[ConsultingMetricResponse]
    warnings: list[str] = Field(default_factory=list)
    model: str


class DailyConsultingNarrative(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1)
    estimatedCauses: list[ConsultingCauseResponse]
    recommendations: list[ConsultingRecommendationResponse]


KEYWORD_TOOLS = {
    "weather_search": ("날씨", "비", "기온", "강수", "weather"),
    "event_search": ("행사", "축제", "이벤트", "event"),
    "closing_sales_forecast": ("마감 매출", "오늘 예상 매출", "closing sales"),
    "tomorrow_visitors_forecast": ("내일 방문", "내일 손님", "tomorrow visitor"),
    "products": ("상품", "메뉴", "가격", "product"),
    "pos_lookup": ("pos", "판매 기록", "거래 내역", "결제 기록"),
    "sales_analysis": ("매출", "객단가", "주문 수", "왜 줄", "왜 늘", "sales"),
}
CHAT_READ_ONLY_TOOLS = frozenset(TOOL_DESCRIPTIONS)


install_browser_log_api(app, "ai-service", require_backend_jwt)


def _allowed_tools(payload: AiChatRequest) -> list[str]:
    return [name for name in payload.availableTools if name in CHAT_READ_ONLY_TOOLS]


def _summary(value: Any, limit: int = 500) -> str:
    rendered = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    return rendered if len(rendered) <= limit else rendered[: limit - 1] + "…"


def _extract_citations(result: Any) -> list[AiCitationResponse]:
    found: list[AiCitationResponse] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            url = value.get("sourceUrl") or value.get("url")
            title = value.get("sourceTitle") or value.get("title")
            if url and title and str(url).startswith(("http://", "https://")):
                candidate = AiCitationResponse(
                    title=str(title),
                    url=str(url),
                    organization=value.get("organization"),
                )
                if all(existing.url != candidate.url for existing in found):
                    found.append(candidate)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(result)
    return found


def _fallback_tool_selection(payload: AiChatRequest, allowed: list[str]) -> list[str]:
    text = payload.message.lower()
    selected = [
        name
        for name in allowed
        if any(keyword in text for keyword in KEYWORD_TOOLS.get(name, ()))
    ]
    # A POS lookup is redundant when sales_analysis already loads the same ledger.
    if "sales_analysis" in selected and "pos_lookup" in selected:
        selected.remove("pos_lookup")
    return selected


def _answer_from_results(message: str, invoked: list[tuple[str, dict[str, Any]]]) -> str:
    successful = [(name, result.get("data")) for name, result in invoked if result.get("ok")]
    if not invoked:
        if any(keyword in message.lower() for keyword in ("컨설팅", "보고서", "저장", "게시", "업로드")):
            return (
                "채팅에서는 요청에 대한 설명과 분석 답변만 제공하며 컨설팅 저장이나 게시를 실행하지 않습니다. "
                "궁금한 매출·운영 내용을 질문하면 사용 가능한 데이터를 근거로 답변하겠습니다."
            )
        return "매장 운영, 매출, 날씨, 행사, 상품 또는 방문자 예측에 대해 질문해 주세요."
    if not successful:
        return "필요한 데이터를 조회하지 못했습니다. 잠시 후 다시 시도해 주세요."

    sentences = []
    for name, data in successful:
        if name == "sales_analysis" and isinstance(data, dict):
            change = data.get("salesChangeRate")
            change_text = "비교 데이터 없음" if change is None else f"직전 7일 대비 {change:+.1f}%"
            sentences.append(
                f"최근 매출은 {int(data.get('totalSales', 0)):,}원이며 {change_text}입니다. "
                f"주문은 {data.get('orderCount', 0)}건입니다."
            )
        elif name == "closing_sales_forecast" and isinstance(data, dict):
            sentences.append(
                f"현재 {int(data.get('observedSalesAmount', 0)):,}원, 마감 예상 매출은 "
                f"{int(data.get('forecastClosingSalesAmount', 0)):,}원입니다."
            )
        elif name == "tomorrow_visitors_forecast" and isinstance(data, dict):
            sentences.append(f"내일 예상 방문자는 {data.get('expectedVisitors', 0)}명입니다.")
        elif name == "weather_search" and isinstance(data, dict):
            sentences.append(
                f"날씨는 {data.get('summary', '정보 없음')}, 기온 {data.get('temperature', '?')}℃, "
                f"강수확률 {data.get('pop', '?')}%입니다."
            )
        elif name == "event_search" and isinstance(data, dict):
            names = [event.get("name") for event in data.get("events", []) if event.get("name")]
            sentences.append("주변 행사: " + (", ".join(names) if names else "예정된 행사가 없습니다."))
        elif name == "products" and isinstance(data, list):
            names = [str(item.get("name")) for item in data[:5]]
            sentences.append("현재 상품: " + ", ".join(names))
        else:
            sentences.append(f"{name} 조회 결과: {_summary(data, 220)}")
    return " ".join(sentences)


def _run_rule_agent(payload: AiChatRequest, allowed: list[str]) -> AiChatResponse:
    tool_map = get_tool_map(
        user_id=payload.userId,
        store_id=payload.storeId,
    )
    selected = _fallback_tool_selection(payload, allowed)
    executions: list[AiToolExecutionResponse] = []
    invoked: list[tuple[str, dict[str, Any]]] = []
    citations: list[AiCitationResponse] = []
    for name in selected:
        started = time.perf_counter()
        try:
            result = tool_map[name]()
        except Exception as exc:  # tool boundary must not crash the agent
            logger.exception("Tool %s failed", name)
            result = {"ok": False, "error": str(exc)}
        elapsed = int((time.perf_counter() - started) * 1000)
        invoked.append((name, result))
        executions.append(
            AiToolExecutionResponse(
                toolName=name,
                status="SUCCESS" if result.get("ok") else "FAILED",
                arguments={"storeId": payload.storeId},
                resultSummary=_summary(result),
                latencyMs=elapsed,
            )
        )
        citations.extend(_extract_citations(result))
    if invoked and not any(result.get("ok") for _, result in invoked):
        raise api_error(502, "TOOL_EXECUTION_ERROR", "필수 Tool 실행에 실패했습니다.")
    finish_reason = "TOOL_ERROR" if any(not result.get("ok") for _, result in invoked) else "STOP"
    return AiChatResponse(
        answer=_answer_from_results(payload.message, invoked),
        usedTools=executions,
        citations=citations,
        model="rule-agent-v1",
        finishReason=finish_reason,
    )


def _build_langchain_model() -> Any | None:
    if not settings.ai_api_key:
        return None
    llm_kwargs: dict[str, Any] = {
        "model": settings.ai_model,
        "api_key": settings.ai_api_key,
        "temperature": 0,
        "timeout": settings.ai_request_timeout_seconds,
    }
    if settings.ai_provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        llm_kwargs["max_tokens"] = 2048
        return ChatAnthropic(**llm_kwargs)
    if settings.ai_provider == "openai":
        from langchain_openai import ChatOpenAI

        if settings.ai_base_url:
            llm_kwargs["base_url"] = settings.ai_base_url
        return ChatOpenAI(**llm_kwargs)
    logger.warning("Unsupported AI provider: %s", settings.ai_provider)
    return None


def _run_langchain_agent(payload: AiChatRequest, allowed: list[str]) -> AiChatResponse | None:
    if not settings.ai_api_key:
        return None
    try:
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

        tools = get_langchain_tools(
            user_id=payload.userId,
            store_id=payload.storeId,
            allowed_tools=allowed,
        )
        tool_map = {tool.name: tool for tool in tools}
        llm = _build_langchain_model()
        if llm is None:
            return None
        model = llm.bind_tools(tools) if tools else llm

        messages: list[Any] = [
            SystemMessage(
                content=(
                    "당신은 ON:CLICK 소상공인 매장 운영 에이전트입니다. 수치가 필요한 질문은 반드시 제공된 도구를 사용하고, "
                    "도구 결과와 관측 사실을 구분하며, 추측을 사실처럼 말하지 마세요. 답변은 한국어로 간결하게 작성하세요. "
                    "채팅에서는 질문에 답변만 하며 컨설팅 생성·저장, 마케팅 생성·승인, Instagram 게시를 절대 실행하지 마세요."
                )
            )
        ]
        messages.append(HumanMessage(content=payload.message))

        executions: list[AiToolExecutionResponse] = []
        citations: list[AiCitationResponse] = []
        for _ in range(4):
            ai_message = model.invoke(messages)
            messages.append(ai_message)
            tool_calls = getattr(ai_message, "tool_calls", []) or []
            if not tool_calls:
                content = ai_message.content
                if isinstance(content, list):
                    content = " ".join(str(block.get("text", block)) if isinstance(block, dict) else str(block) for block in content)
                answer = str(content).strip()
                if not answer:
                    raise api_error(422, "AI_RESPONSE_INVALID", "AI가 유효한 응답을 생성하지 못했습니다.")
                if executions and all(execution.status == "FAILED" for execution in executions):
                    raise api_error(502, "TOOL_EXECUTION_ERROR", "필수 Tool 실행에 실패했습니다.")
                finish_reason = "TOOL_ERROR" if any(execution.status == "FAILED" for execution in executions) else "STOP"
                return AiChatResponse(
                    answer=answer,
                    usedTools=executions,
                    citations=citations,
                    model=settings.ai_model,
                    finishReason=finish_reason,
                )
            for call in tool_calls:
                name = call.get("name")
                call_id = call.get("id")
                arguments = call.get("args") or {}
                
                # name과 id가 없으면 skip (무효한 tool_calls)
                if not name or not call_id:
                    logger.warning("Skipping tool_call with missing name or id: %s", call)
                    continue
                
                started = time.perf_counter()
                try:
                    # tool_map에 없는 도구인지 확인
                    if name not in tool_map:
                        raise KeyError(f"Tool '{name}' not found in tool_map")
                    result = tool_map[name].invoke(arguments)
                except Exception as exc:
                    result = {"ok": False, "error": str(exc)}
                elapsed = int((time.perf_counter() - started) * 1000)
                executions.append(
                    AiToolExecutionResponse(
                        toolName=name,
                        status="SUCCESS" if result.get("ok") else "FAILED",
                        arguments=arguments,
                        resultSummary=_summary(result),
                        latencyMs=elapsed,
                    )
                )
                citations.extend(_extract_citations(result))
                messages.append(
                    ToolMessage(
                        content=_summary(result, 4000),
                        tool_call_id=call_id,
                    )
                )
        return AiChatResponse(
            answer="도구 호출 횟수 제한에 도달했습니다.",
            usedTools=executions,
            citations=citations,
            model=settings.ai_model,
            finishReason="MAX_TOKENS",
        )
    except HTTPException:
        raise
    except Exception as exc:
        if isinstance(exc, TimeoutError) or "timeout" in exc.__class__.__name__.lower():
            raise api_error(504, "AI_TIMEOUT", "AI 처리 시간이 초과되었습니다.")
        logger.exception("LangChain agent failed; falling back to the local agent")
        return None


def _handle_chat(payload: AiChatRequest, authorization: str | None) -> AiChatResponse:
    require_backend_jwt(authorization)
    allowed = _allowed_tools(payload)
    return _run_langchain_agent(payload, allowed) or _run_rule_agent(payload, allowed)


def _write_daily_report_with_llm(report: dict[str, Any]) -> dict[str, Any]:
    llm = _build_langchain_model()
    if llm is None:
        return report
    from langchain_core.messages import HumanMessage, SystemMessage

    evidence = {
        "targetDate": str(report["targetDate"]),
        "chatInsights": report["chatInsights"],
        "keyMetrics": report["keyMetrics"],
        "externalFactors": report["externalFactors"],
        "warnings": report["warnings"],
    }
    started = time.perf_counter()
    try:
        writer = llm.with_structured_output(DailyConsultingNarrative)
        narrative = writer.invoke(
            [
                SystemMessage(
                    content=(
                        "당신은 ON:CLICK 소상공인 일일 컨설팅 보고서 작성자입니다. 제공된 evidence만 사용하고, "
                        "존재하지 않는 수치·행사·원인을 만들지 마세요. summary는 2~4문장, 원인은 근거와 confidence를 포함하고, "
                        "추천은 내일 바로 실행 가능한 행동으로 작성하세요. FORECAST_CLOSING_TOTAL_SALES는 추가 매출이 아니라 "
                        "오늘의 최종 마감 예상 총액이고, TOMORROW_EXPECTED_VISITORS는 내일 방문자 예측입니다. 행사 날짜가 "
                        "targetDate와 다르면 오늘 행사라고 표현하지 마세요. RECENT_7D 지표는 오늘 하루가 아니라 최근 7일과 "
                        "직전 7일의 비교입니다. 미래 예측값은 과거 매출 감소의 원인으로 사용하지 말고 추천 계획에만 사용하세요. "
                        "모든 문장은 자연스러운 한국어로 작성하세요."
                    )
                ),
                HumanMessage(content=json.dumps(evidence, ensure_ascii=False, default=str)),
            ]
        )
        if isinstance(narrative, dict):
            narrative = DailyConsultingNarrative.model_validate(narrative)
        report["summary"] = narrative.summary
        report["estimatedCauses"] = [
            item.model_dump(mode="json") for item in narrative.estimatedCauses
        ]
        report["recommendations"] = [
            item.model_dump(mode="json") for item in narrative.recommendations
        ]
        report["model"] = settings.ai_model
        report["usedTools"].append(
            {
                "toolName": "claude_report_writer",
                "status": "SUCCESS",
                "arguments": {"model": settings.ai_model},
                "resultSummary": "근거 기반 일일 보고서 서술 생성 완료",
                "latencyMs": int((time.perf_counter() - started) * 1000),
            }
        )
    except Exception as exc:
        logger.exception("Daily report LLM writer failed; using deterministic report")
        report["warnings"].append("Claude 보고서 서술 생성에 실패해 규칙 기반 보고서를 사용했습니다.")
        report["usedTools"].append(
            {
                "toolName": "claude_report_writer",
                "status": "FAILED",
                "arguments": {"model": settings.ai_model},
                "resultSummary": str(exc)[:500],
                "latencyMs": int((time.perf_counter() - started) * 1000),
            }
        )
    report["content"] = render_daily_content(report)
    return report


def _generated_text(value: Any) -> str:
    content = getattr(value, "content", value)
    if isinstance(content, list):
        content = " ".join(
            str(block.get("text", "")) if isinstance(block, dict) else str(block)
            for block in content
        )
    return str(content).strip()


def _load_image_block(url: str) -> dict[str, str]:
    parsed = urlparse(url)
    is_backend_media = parsed.path.startswith("/public/media/")
    download_url = (
        urljoin(f"{settings.api_base_url}/", parsed.path.lstrip("/"))
        if is_backend_media
        else url
    )
    download_parsed = urlparse(download_url)
    target = safe_upstream_target(download_url)
    started = time.perf_counter()
    if not download_parsed.hostname or (not is_backend_media and download_parsed.scheme != "https"):
        raise ValueError("INVALID_MARKETING_IMAGE_URL")
    if not is_backend_media:
        try:
            addresses = {
                item[4][0]
                for item in socket.getaddrinfo(download_parsed.hostname, download_parsed.port or 443)
            }
        except socket.gaierror as exc:
            raise ValueError("MARKETING_IMAGE_DNS_FAILED") from exc
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                raise ValueError("MARKETING_IMAGE_URL_BLOCKED")

    headers = {"User-Agent": "on-click-ai/1.0 (marketing-image-fetcher)"}
    if is_backend_media:
        from src.auth import backend_authorization_header

        headers.update(backend_authorization_header())

    log_event(
        logger,
        logging.INFO,
        "marketing.image_download.started",
        upstreamService="MARKETING_IMAGE_HOST",
        upstreamTarget=target,
    )
    try:
        with requests.get(
            download_url,
            headers=request_headers(headers),
            stream=True,
            allow_redirects=False,
            timeout=settings.request_timeout_seconds,
        ) as response:
            response.raise_for_status()
            mime_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
            if mime_type not in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
                raise ValueError("MARKETING_IMAGE_TYPE_UNSUPPORTED")
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                size += len(chunk)
                if size > 5 * 1024 * 1024:
                    raise ValueError("MARKETING_IMAGE_TOO_LARGE")
                chunks.append(chunk)
    except requests.RequestException as exc:
        if isinstance(exc, requests.Timeout):
            error_code = "MARKETING_IMAGE_DOWNLOAD_TIMEOUT"
        elif isinstance(exc, requests.ConnectionError):
            error_code = "MARKETING_IMAGE_CONNECTION_FAILED"
        elif isinstance(exc, requests.HTTPError):
            error_code = "MARKETING_IMAGE_HTTP_ERROR"
        else:
            error_code = "MARKETING_IMAGE_DOWNLOAD_FAILED"
        log_event(
            logger,
            logging.ERROR,
            "marketing.image_download.failed",
            upstreamService="MARKETING_IMAGE_HOST",
            upstreamTarget=target,
            errorCode=error_code,
            exceptionType=exc.__class__.__name__,
            exc_info=True,
        )
        raise ValueError(error_code) from exc
    if not chunks:
        raise ValueError("MARKETING_IMAGE_EMPTY")
    log_event(
        logger,
        logging.INFO,
        "marketing.image_download.completed",
        upstreamService="MARKETING_IMAGE_HOST",
        upstreamTarget=target,
        contentType=mime_type,
        bytesDownloaded=size,
        latencyMs=round((time.perf_counter() - started) * 1000, 2),
    )
    return {
        "type": "image",
        "base64": base64.b64encode(b"".join(chunks)).decode("ascii"),
        "mime_type": mime_type,
    }


def _generate_marketing_copy(payload: GenerateMarketingCopyRequest) -> GenerateMarketingCopyResponse:
    tags = [tag if tag.startswith("#") else f"#{tag}" for tag in payload.tags]
    llm = _build_langchain_model()
    if llm is None:
        fallback = "\n\n".join(part for part in [payload.draftText, " ".join(tags)] if part)
        return GenerateMarketingCopyResponse(content=fallback[:2200], model="rule-copy-v1")

    from langchain_core.messages import HumanMessage, SystemMessage

    request_text = {
        "draftText": payload.draftText,
        "tags": tags,
        "tone": payload.tone,
        "additionalRequest": payload.additionalRequest,
    }
    blocks: list[dict[str, Any]] = [
        {"type": "text", "text": json.dumps(request_text, ensure_ascii=False)}
    ]
    blocks.extend(_load_image_block(url) for url in payload.imageUrls)
    response = llm.invoke(
        [
            SystemMessage(
                content=(
                    "사용자가 제공한 이미지, 초안, 태그에 맞는 Instagram 게시 글 하나만 작성하세요. "
                    "컨설팅·매출·날씨·행사 등 제공되지 않은 사실을 추가하지 말고 이미지도 생성하지 마세요. "
                    "제목, 설명, 마크다운 코드 블록 없이 바로 게시할 자연스러운 한국어 본문을 작성하고, "
                    "제공된 태그만 글 마지막에 붙이세요. 전체 길이는 2200자 이하입니다."
                )
            ),
            HumanMessage(content=blocks),
        ]
    )
    content = _generated_text(response)
    if not content:
        raise ValueError("MARKETING_COPY_GENERATION_FAILED")
    return GenerateMarketingCopyResponse(content=content[:2200], model=settings.ai_model)


@app.post("/ai/chat", response_model=AiChatResponse)
def ai_chat(
    payload: AiChatRequest,
    authorization: str | None = Header(default=None),
) -> AiChatResponse:
    return _handle_chat(payload, authorization)


@app.post("/run_agent", response_model=AiChatResponse, include_in_schema=False)
def run_agent(
    payload: AiChatRequest,
    authorization: str | None = Header(default=None),
) -> AiChatResponse:
    return _handle_chat(payload, authorization)


@app.post("/ai/consultings", response_model=GenerateConsultingResponse)
def generate_consulting(
    payload: GenerateConsultingRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_backend_jwt(authorization)
    try:
        return analyze_consulting(payload.model_dump(mode="json"))
    except ValueError as exc:
        error_code = str(exc)
        if error_code == "INVALID_ANALYSIS_PERIOD":
            raise api_error(400, error_code, "분석 기간이 올바르지 않습니다.")
        if error_code == "INSUFFICIENT_SALES_DATA":
            raise api_error(422, error_code, "분석할 매출 데이터가 부족합니다.")
        raise api_error(422, "CONSULTING_GENERATION_FAILED", "컨설팅 결과 생성에 실패했습니다.")


@app.post("/ai/consultings/daily", response_model=GenerateDailyConsultingResponse)
def generate_daily_consulting(
    payload: GenerateDailyConsultingRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_backend_jwt(authorization)
    try:
        report = build_daily_consulting(payload.model_dump(mode="json"))
        return _write_daily_report_with_llm(report)
    except ValueError as exc:
        if str(exc) == "STORE_OR_CHAT_CONTEXT_NOT_FOUND":
            raise api_error(404, str(exc), "대상 매장 또는 필요한 대화 문맥을 찾을 수 없습니다.")
        raise api_error(422, "DAILY_CONSULTING_GENERATION_FAILED", "일일 보고서 생성에 실패했습니다.")
    except RuntimeError as exc:
        if str(exc) == "TOOL_EXECUTION_ERROR":
            raise api_error(502, str(exc), "보고서 필수 데이터 Tool 실행에 실패했습니다.")
        raise api_error(422, "DAILY_CONSULTING_GENERATION_FAILED", "일일 보고서 생성에 실패했습니다.")


@app.post("/ai/forecasts/closing-sales", response_model=ClosingSalesForecastResponse)
def forecast_closing_sales(
    payload: ClosingSalesForecastRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_backend_jwt(authorization)
    transactions = [item.model_dump(mode="json") for item in payload.salesData]
    if not any(item["status"] == "COMPLETED" for item in transactions):
        raise api_error(422, "INSUFFICIENT_FORECAST_DATA", "예측 가능한 완료 거래가 없습니다.")
    try:
        as_of = payload.asOf.replace(tzinfo=None)
        result = predict_closing_sales(transactions, as_of)
        return {
            "storeId": payload.storeId,
            "businessDate": as_of.date(),
            "currency": "KRW",
            "observedSalesAmount": result["observedSalesAmount"],
            "forecastClosingSalesAmount": result["forecastClosingSalesAmount"],
            "model": result["model"],
            "sampleDays": result["sampleDays"],
            "generatedAt": datetime.now(timezone.utc),
        }
    except (KeyError, TypeError, ValueError):
        raise api_error(500, "FORECAST_EXECUTION_FAILED", "마감 매출 예측 실행에 실패했습니다.")


@app.post("/ai/forecasts/tomorrow-visitors", response_model=TomorrowVisitorsForecastResponse)
def forecast_tomorrow_visitors(
    payload: TomorrowVisitorsForecastRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_backend_jwt(authorization)
    transactions = [item.model_dump(mode="json") for item in payload.salesData]
    if not any(item["status"] == "COMPLETED" for item in transactions):
        raise api_error(422, "INSUFFICIENT_FORECAST_DATA", "예측 가능한 완료 거래가 없습니다.")
    try:
        result = predict_tomorrow_visitors(transactions, payload.baseDate)
        return {
            "storeId": payload.storeId,
            "targetDate": result["targetDate"],
            "expectedVisitors": result["expectedVisitors"],
            "model": result["model"],
            "sampleDays": result["sampleDays"],
            "generatedAt": datetime.now(timezone.utc),
        }
    except (KeyError, TypeError, ValueError):
        raise api_error(500, "FORECAST_EXECUTION_FAILED", "내일 방문자 예측 실행에 실패했습니다.")


@app.post("/ai/marketings/copy", response_model=GenerateMarketingCopyResponse)
def generate_marketing_copy(
    payload: GenerateMarketingCopyRequest,
    authorization: str | None = Header(default=None),
) -> GenerateMarketingCopyResponse:
    require_backend_jwt(authorization)
    try:
        return _generate_marketing_copy(payload)
    except Exception as exc:
        image_failures: dict[str, tuple[int, str, bool]] = {
            "INVALID_MARKETING_IMAGE_URL": (422, "이미지 URL 형식이 올바르지 않습니다.", False),
            "MARKETING_IMAGE_URL_BLOCKED": (422, "내부망 또는 허용되지 않은 이미지 URL입니다.", False),
            "MARKETING_IMAGE_DNS_FAILED": (502, "이미지 호스트의 주소를 확인할 수 없습니다.", True),
            "MARKETING_IMAGE_DOWNLOAD_TIMEOUT": (504, "이미지 다운로드 시간이 초과되었습니다.", True),
            "MARKETING_IMAGE_CONNECTION_FAILED": (502, "이미지 호스트에 연결할 수 없습니다.", True),
            "MARKETING_IMAGE_HTTP_ERROR": (502, "이미지 호스트가 오류 응답을 반환했습니다.", True),
            "MARKETING_IMAGE_DOWNLOAD_FAILED": (502, "이미지를 다운로드하지 못했습니다.", True),
            "MARKETING_IMAGE_TYPE_UNSUPPORTED": (422, "지원하지 않는 이미지 형식입니다.", False),
            "MARKETING_IMAGE_TOO_LARGE": (413, "이미지는 파일당 5MB 이하여야 합니다.", False),
            "MARKETING_IMAGE_EMPTY": (422, "빈 이미지 파일은 사용할 수 없습니다.", False),
        }
        reason_code = str(exc)
        if isinstance(exc, ValueError) and reason_code in image_failures:
            status_code, message, retryable = image_failures[reason_code]
            raise api_error(
                status_code,
                reason_code,
                message,
                details={"imageCount": len(payload.imageUrls), "stage": "image_download"},
                retryable=retryable,
            )

        exception_name = exc.__class__.__name__.lower()
        if isinstance(exc, TimeoutError) or "timeout" in exception_name:
            status_code, error_code, retryable = 504, "AI_PROVIDER_TIMEOUT", True
            message = "AI 제공자 응답 시간이 초과되었습니다."
        elif "authentication" in exception_name or "permission" in exception_name:
            status_code, error_code, retryable = 502, "AI_PROVIDER_AUTHENTICATION_FAILED", False
            message = "AI 제공자 인증에 실패했습니다. 서버 API Key 설정을 확인해 주세요."
        elif "ratelimit" in exception_name or "rate_limit" in exception_name:
            status_code, error_code, retryable = 503, "AI_PROVIDER_RATE_LIMITED", True
            message = "AI 제공자 요청 한도를 초과했습니다. 잠시 후 다시 시도해 주세요."
        elif "connection" in exception_name:
            status_code, error_code, retryable = 502, "AI_PROVIDER_CONNECTION_FAILED", True
            message = "AI 제공자에 연결할 수 없습니다."
        elif "badrequest" in exception_name or "bad_request" in exception_name:
            status_code, error_code, retryable = 422, "AI_PROVIDER_REJECTED_REQUEST", False
            message = "AI 제공자가 마케팅 문구 요청을 처리하지 못했습니다."
        else:
            status_code, error_code, retryable = 422, "MARKETING_COPY_GENERATION_FAILED", False
            message = "마케팅 문구 생성에 실패했습니다."
        log_event(
            logger,
            logging.ERROR,
            "marketing.copy.failed",
            errorCode=error_code,
            aiProvider=settings.ai_provider,
            aiModel=settings.ai_model,
            exceptionType=exc.__class__.__name__,
            exc_info=True,
        )
        raise api_error(
            status_code,
            error_code,
            message,
            details={
                "provider": settings.ai_provider,
                "model": settings.ai_model,
                "stage": "copy_generation",
            },
            retryable=retryable,
        )


@app.post(
    "/ai/marketings/{marketingId}/publish/instagram",
    response_model=PublishInstagramResponse,
)
def publish_instagram(
    marketingId: int,
    payload: PublishInstagramRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_backend_jwt(authorization)
    try:
        publish_payload = payload.model_dump(mode="json")
        publish_payload["instagramPassword"] = payload.instagramPassword.get_secret_value()
        return publish_instagram_post(
            marketingId,
            publish_payload,
        )
    except KeyError as exc:
        if exc.args and exc.args[0] == "DUPLICATE_PUBLISH_REQUEST":
            raise api_error(409, "DUPLICATE_PUBLISH_REQUEST", "이미 처리된 게시 요청입니다.")
        raise
    except InstagramCredentialsInvalid:
        raise api_error(
            422,
            "INSTAGRAM_CREDENTIALS_INVALID",
            "Instagram 로그인 정보가 올바르지 않습니다.",
            details={"stage": "instagram_login", "marketingId": marketingId},
            retryable=False,
        )
    except InstagramLoginChallengeRequired:
        raise api_error(
            422,
            "INSTAGRAM_LOGIN_CHALLENGE_REQUIRED",
            "Instagram 추가 인증 또는 사용자 확인이 필요합니다.",
            details={"stage": "instagram_login", "marketingId": marketingId},
            retryable=False,
        )
    except InstagramProviderTimeout:
        raise api_error(
            504,
            "INSTAGRAM_PUBLISH_TIMEOUT",
            "Instagram 게시 시간이 초과되었습니다.",
            details={"stage": "browser_publish", "marketingId": marketingId},
            retryable=True,
        )
    except BrowserMCPUnavailable as exc:
        details = {
            "stage": "browser_publish",
            "marketingId": marketingId,
            "reasonCode": exc.reason_code,
        }
        if exc.upstream_error_code:
            details["upstreamErrorCode"] = exc.upstream_error_code
        raise api_error(
            502,
            "BROWSER_MCP_UNAVAILABLE",
            "Browser MCP 게시 Tool 호출에 실패했습니다.",
            details=details,
            retryable=exc.reason_code
            not in {"BROWSER_MCP_NOT_CONFIGURED", "BROWSER_MCP_CLIENT_NOT_INSTALLED"},
        )
