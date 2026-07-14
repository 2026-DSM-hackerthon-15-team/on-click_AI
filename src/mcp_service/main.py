import hmac
from datetime import datetime, timedelta

from fastapi import FastAPI, Header, Query

from src.errors import api_error
from src.observability import install_browser_log_api, install_observability
from src.settings import settings

app = FastAPI(title="mcp-service")
install_observability(app, "mcp-service")


def _require_internal_key(value: str | None) -> None:
    if not value or not hmac.compare_digest(value, settings.internal_api_key):
        raise api_error(401, "INVALID_INTERNAL_API_KEY", "내부 API Key가 올바르지 않습니다.")


install_browser_log_api(app, "mcp-service", _require_internal_key)


@app.get("/weather")
def weather(region: str = Query(min_length=1), industry: str = Query(min_length=1)):
    # PoC: return a strict, simple JSON schema
    now = datetime.utcnow()
    data = {
        "fetchedAt": now.isoformat() + "Z",
        "location": {"region": region, "industry": industry},
        "summary": "대체로 맑음",
        "temperature": 25.3,
        "pop": 10,  # precipitation probability
        "confidence": 0.9,
        "source": "mock"
    }
    return data


@app.get("/events")
def events(
    days: int = Query(default=7, ge=1, le=30),
    region: str = Query(min_length=1),
    industry: str = Query(min_length=1),
):
    # Return nearby events mock
    today = datetime.utcnow().date()
    events = [
        {"name": "지역 축제", "date": str(today + timedelta(days=3)), "impact": 0.15},
    ]
    return {
        "events": events,
        "location": {"region": region, "industry": industry},
        "source": "mock",
        "fetchedAt": datetime.utcnow().isoformat() + "Z",
    }
