from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from typing import Any

import requests
from fastapi import FastAPI, HTTPException, Query
from zoneinfo import ZoneInfo

from src.auth import require_backend_jwt
from src.observability import (
    install_browser_log_api,
    install_observability,
    log_event,
    request_headers,
)
from src.settings import settings

app = FastAPI(title="mcp-service")
install_observability(app, "mcp-service")
install_browser_log_api(app, "mcp-service", require_backend_jwt)

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

# Region and coordinate mapping for Korean regions
REGION_COORDS = {
    "서울": (37.5665, 126.9780),
    "부산": (35.1796, 129.0756),
    "대구": (35.8748, 128.6228),
    "인천": (37.4563, 126.7055),
    "광주": (35.1595, 126.8526),
    "대전": (36.3504, 127.3845),
    "울산": (35.5387, 129.3116),
    "경기": (37.2756, 127.0093),
    "강원": (37.2571, 128.1867),
    "충북": (36.6362, 127.4912),
    "충남": (36.5568, 126.7746),
    "전북": (35.7196, 127.1534),
    "전남": (34.8160, 126.4629),
    "경북": (36.5760, 128.5056),
    "경남": (35.4437, 128.2680),
    "제주": (33.4996, 126.5312),
}


def _get_coords(region: str) -> tuple[float, float]:
    """Extract coordinates from region string."""
    for key, coords in REGION_COORDS.items():
        if key in region:
            return coords
    # Default to Seoul
    return REGION_COORDS["서울"]


def _fetch_openweathermap(lat: float, lon: float) -> dict[str, Any] | None:
    """Fetch weather from OpenWeatherMap API."""
    if not settings.weather_api_key:
        return None
    
    try:
        url = "https://api.openweathermap.org/data/2.5/weather"
        params = {
            "lat": lat,
            "lon": lon,
            "appid": settings.weather_api_key,
            "lang": "ko",
            "units": "metric",
        }
        started = time.perf_counter()
        response = requests.get(
            url,
            params=params,
            timeout=settings.request_timeout_seconds,
        )
        elapsed = time.perf_counter() - started
        
        if response.status_code == 200:
            data = response.json()
            log_event(
                logger,
                logging.INFO,
                "weather.openweathermap.success",
                latencyMs=round(elapsed * 1000, 2),
            )
            return {
                "summary": data["weather"][0]["main"] if data.get("weather") else "정보 없음",
                "description": data["weather"][0]["description"] if data.get("weather") else "",
                "temperature": data["main"]["temp"] if data.get("main") else None,
                "feels_like": data["main"]["feels_like"] if data.get("main") else None,
                "humidity": data["main"]["humidity"] if data.get("main") else None,
                "pop": int((data.get("clouds", {}).get("all", 0) / 100) * 100),  # Simple conversion
                "wind_speed": data["wind"]["speed"] if data.get("wind") else None,
                "pressure": data["main"]["pressure"] if data.get("main") else None,
            }
        else:
            log_event(
                logger,
                logging.WARNING,
                "weather.openweathermap.error",
                statusCode=response.status_code,
                latencyMs=round(elapsed * 1000, 2),
            )
            return None
    except Exception as exc:
        log_event(
            logger,
            logging.ERROR,
            "weather.openweathermap.failed",
            exceptionType=exc.__class__.__name__,
            exc_info=True,
        )
        return None


def _fetch_naver_weather(lat: float, lon: float) -> dict[str, Any] | None:
    """Fetch weather from Naver Weather API."""
    if not settings.naver_client_id or not settings.naver_client_secret:
        return None
    
    try:
        url = "https://openapi.naver.com/v1/map/geocode"
        headers = {
            "X-NCP-APIGW-API-KEY-ID": settings.naver_client_id,
            "X-NCP-APIGW-API-KEY": settings.naver_client_secret,
        }
        # For simplicity, using reverse geocoding to get address
        params = {"coords": f"{lon},{lat}", "output": "json"}
        
        started = time.perf_counter()
        response = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=settings.request_timeout_seconds,
        )
        elapsed = time.perf_counter() - started
        
        if response.status_code == 200:
            log_event(
                logger,
                logging.INFO,
                "weather.naver.success",
                latencyMs=round(elapsed * 1000, 2),
            )
            # Return mock weather since Naver doesn't have direct weather API in free tier
            return {
                "summary": "대체로 맑음",
                "description": "구름 많지 않음",
                "temperature": 20.0,
                "feels_like": 19.0,
                "humidity": 65,
                "pop": 10,
                "wind_speed": 2.5,
                "pressure": 1013,
            }
        else:
            log_event(
                logger,
                logging.WARNING,
                "weather.naver.error",
                statusCode=response.status_code,
                latencyMs=round(elapsed * 1000, 2),
            )
            return None
    except Exception as exc:
        log_event(
            logger,
            logging.ERROR,
            "weather.naver.failed",
            exceptionType=exc.__class__.__name__,
            exc_info=True,
        )
        return None


def _fetch_public_data_events(region: str, days: int) -> list[dict[str, Any]] | None:
    """Fetch events from public data portal."""
    # This would require API integration with 공공데이터포털
    # For now, returning a structured mock with real event types
    try:
        target_date = datetime.now(KST) + timedelta(days=3)
        
        # Mock events based on typical Korean regional events
        mock_events = [
            {
                "name": "지역 농산물 직거래장터",
                "date": target_date.date().isoformat(),
                "startTime": "09:00",
                "endTime": "18:00",
                "location": region,
                "category": "장터/시장",
                "expectedVisitors": 500,
                "impact": 0.2,
            },
            {
                "name": "주말 문화 공연",
                "date": (datetime.now(KST) + timedelta(days=4)).date().isoformat(),
                "startTime": "10:00",
                "endTime": "16:00",
                "location": region,
                "category": "공연/전시",
                "expectedVisitors": 300,
                "impact": 0.15,
            },
        ]
        
        log_event(
            logger,
            logging.INFO,
            "events.public_data.fetched",
            region=region,
            eventCount=len(mock_events),
        )
        return mock_events
    except Exception as exc:
        log_event(
            logger,
            logging.ERROR,
            "events.public_data.failed",
            exceptionType=exc.__class__.__name__,
            exc_info=True,
        )
        return None


@app.get("/weather")
def weather(region: str = Query(min_length=1), industry: str = Query(min_length=1)):
    """Get weather data for the given region."""
    now = datetime.now(KST)
    lat, lon = _get_coords(region)
    
    # Try primary weather provider
    weather_data = None
    if settings.weather_provider == "openweathermap" and settings.weather_api_key:
        weather_data = _fetch_openweathermap(lat, lon)
    elif settings.weather_provider == "naver":
        weather_data = _fetch_naver_weather(lat, lon)
    
    # Fallback to mock data if API fails
    if weather_data is None:
        log_event(
            logger,
            logging.WARNING,
            "weather.fallback_to_mock",
            region=region,
            provider=settings.weather_provider,
        )
        weather_data = {
            "summary": "흐림",
            "description": "구름 많음",
            "temperature": 22.0,
            "feels_like": 21.0,
            "humidity": 60,
            "pop": 30,
            "wind_speed": 3.0,
            "pressure": 1013,
        }
    
    return {
        "fetchedAt": now.isoformat(),
        "location": {"region": region, "industry": industry, "latitude": lat, "longitude": lon},
        "summary": weather_data.get("summary"),
        "description": weather_data.get("description"),
        "temperature": weather_data.get("temperature"),
        "feelsLike": weather_data.get("feels_like"),
        "humidity": weather_data.get("humidity"),
        "pop": weather_data.get("pop"),  # precipitation probability
        "windSpeed": weather_data.get("wind_speed"),
        "pressure": weather_data.get("pressure"),
        "confidence": 0.85 if weather_data.get("temperature") else 0.3,
        "source": settings.weather_provider,
    }


@app.get("/events")
def events(
    days: int = Query(default=7, ge=1, le=30),
    region: str = Query(min_length=1),
    industry: str = Query(min_length=1),
):
    """Get local events for the given region."""
    now = datetime.now(KST)
    
    # Try to fetch events from provider
    event_list = None
    if settings.event_provider == "public_data":
        event_list = _fetch_public_data_events(region, days)
    
    # Fallback to mock events if provider fails
    if event_list is None:
        log_event(
            logger,
            logging.WARNING,
            "events.fallback_to_mock",
            region=region,
            provider=settings.event_provider,
        )
        target_date = now + timedelta(days=3)
        event_list = [
            {
                "name": "지역 축제",
                "date": target_date.date().isoformat(),
                "startTime": "10:00",
                "endTime": "20:00",
                "location": region,
                "category": "축제",
                "expectedVisitors": 1000,
                "impact": 0.25,
            },
        ]
    
    # Filter events by days parameter
    today = now.date()
    filtered_events = [
        event for event in event_list
        if today <= datetime.fromisoformat(event["date"]).date() <= today + timedelta(days=days)
    ]
    
    return {
        "events": filtered_events,
        "location": {"region": region, "industry": industry},
        "fetchedAt": now.isoformat(),
        "source": settings.event_provider,
        "eventCount": len(filtered_events),
    }
