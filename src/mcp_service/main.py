from fastapi import FastAPI, Query
from datetime import datetime, timedelta

from src.observability import install_observability

app = FastAPI(title="mcp-service")
install_observability(app, "mcp-service")


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
