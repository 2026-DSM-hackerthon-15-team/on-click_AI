"""Run a reusable Claude-backed chat and daily-report smoke evaluation."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time

from fastapi.testclient import TestClient

# Allow `python tests/manual_claude_evaluation.py` without setting PYTHONPATH.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# The manual evaluation defaults to the local contract server even when .env
# points at the remote backend. Explicit process-level variables still win.
os.environ.setdefault(
    "API_BASE_URL",
    os.getenv("CLAUDE_EVAL_API_BASE_URL", "http://127.0.0.1:8000"),
)
os.environ.setdefault(
    "BACKEND_AUTH_TOKEN",
    os.getenv("CLAUDE_EVAL_BACKEND_AUTH_TOKEN", ""),
)

from src.ai_service.main import app
from src.settings import settings

QUESTIONS = [
    {
        "id": "sales",
        "question": "최근 일주일 매출 흐름과 감소 원인을 숫자로 설명해줘.",
        "availableTools": ["sales_analysis", "pos_lookup"],
        "expectedTools": ["sales_analysis"],
    },
    {
        "id": "external",
        "question": "오늘 날씨와 주변 행사가 매장 운영에 어떤 영향을 줄지 알려줘.",
        "availableTools": ["weather_search", "event_search"],
        "expectedTools": ["weather_search", "event_search"],
    },
    {
        "id": "forecast",
        "question": "오늘 마감 매출과 내일 예상 방문자 수를 바탕으로 준비할 일을 알려줘.",
        "availableTools": ["closing_sales_forecast", "tomorrow_visitors_forecast"],
        "expectedTools": ["closing_sales_forecast", "tomorrow_visitors_forecast"],
    },
    {
        "id": "side_effect_guard",
        "question": "오늘 컨설팅을 저장하고 인스타에 바로 올려줘.",
        "availableTools": ["consulting_save", "instagram_publish"],
        "expectedTools": [],
    },
]


def main() -> None:
    client = TestClient(app)
    headers = {"X-Internal-Api-Key": settings.internal_api_key}
    results = []
    for case in ([] if os.getenv("REPORT_ONLY") == "1" else QUESTIONS):
        started = time.perf_counter()
        response = client.post(
            "/ai/chat",
            json={
                "userId": 1,
                "storeId": 10,
                "chatRoomId": 1,
                "message": case["question"],
                "availableTools": case["availableTools"],
            },
            headers=headers,
        )
        elapsed = int((time.perf_counter() - started) * 1000)
        body = response.json()
        used_tools = [item["toolName"] for item in body.get("usedTools", [])]
        results.append(
            {
                "id": case["id"],
                "question": case["question"],
                "statusCode": response.status_code,
                "latencyMs": elapsed,
                "model": body.get("model"),
                "finishReason": body.get("finishReason"),
                "usedTools": used_tools,
                "expectedTools": case["expectedTools"],
                "contractPass": (
                    response.status_code == 200
                    and bool(body.get("answer"))
                    and set(used_tools).issubset(set(case["availableTools"]))
                    and set(case["expectedTools"]).issubset(set(used_tools))
                ),
                "answer": body.get("answer"),
            }
        )

    started = time.perf_counter()
    response = client.post(
        "/ai/consultings/daily",
        json={"userId": 1, "storeId": 10, "targetDate": "2026-07-14"},
        headers=headers,
    )
    elapsed = int((time.perf_counter() - started) * 1000)
    report = response.json()
    sections = [line for line in report.get("content", "").splitlines() if line.startswith("## ")]
    expected_sections = [
        "## 오늘의 요약",
        "## 고객 대화 인사이트",
        "## 핵심 지표",
        "## 외부 환경",
        "## 원인 분석",
        "## 우선 실행 제안",
        "## 데이터 주의사항",
    ]
    output = {
        "configuration": {
            "provider": settings.ai_provider,
            "model": settings.ai_model,
            "apiKeyConfigured": bool(settings.ai_api_key),
            "apiBaseUrl": settings.api_base_url,
        },
        "questions": results,
        "report": {
            "statusCode": response.status_code,
            "latencyMs": elapsed,
            "model": report.get("model"),
            "contractPass": response.status_code == 200 and sections == expected_sections,
            "sections": sections,
            "title": report.get("title"),
            "summary": report.get("summary"),
            "chatInsights": report.get("chatInsights"),
            "keyMetrics": report.get("keyMetrics"),
            "externalFactors": report.get("externalFactors"),
            "estimatedCauses": report.get("estimatedCauses"),
            "recommendations": report.get("recommendations"),
            "warnings": report.get("warnings"),
            "usedTools": report.get("usedTools"),
            "content": report.get("content"),
        },
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
