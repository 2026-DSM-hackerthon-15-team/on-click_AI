"""
MCP Service weather and events endpoints test
실제 API 통합 테스트
"""
import json
import os
from unittest.mock import Mock, patch

import requests

# 테스트할 환경 설정
os.environ.setdefault("WEATHER_PROVIDER", "openweathermap")
os.environ.setdefault("EVENT_PROVIDER", "public_data")

from src.mcp_service.main import app, weather, events, _fetch_public_data_events


def test_weather_with_mock():
    """테스트 1: 날씨 API 호출 (실제 OpenWeatherMap 없을 때는 fallback)"""
    print("\n=== 테스트 1: 날씨 데이터 조회 (Fallback) ===")
    try:
        result = weather(region="서울특별시 강남구", industry="CAFE")
        
        if result:
            print(f"✅ 날씨 데이터 조회 성공")
            print(f"   지역: {result['location']['region']}")
            print(f"   온도: {result['temperature']}°C")
            print(f"   요약: {result['summary']}")
            print(f"   강수확률: {result['pop']}%")
            print(f"   출처: {result['source']}")
            print(f"   신뢰도: {result['confidence']}")
        else:
            print(f"❌ 날씨 데이터 조회 실패")
    except Exception as e:
        print(f"❌ 에러 발생: {type(e).__name__}: {e}")


def test_events():
    """테스트 2: 행사 정보 조회"""
    print("\n=== 테스트 2: 행사 정보 조회 ===")
    try:
        result = events(days=7, region="서울특별시 강남구", industry="CAFE")
        
        if result:
            print(f"✅ 행사 정보 조회 성공")
            print(f"   지역: {result['location']['region']}")
            print(f"   조회 기간: 7일")
            print(f"   발견된 행사 수: {result['eventCount']}")
            
            for i, event in enumerate(result['events'][:3], 1):
                print(f"   행사 {i}: {event['name']} ({event['date']})")
                if 'expectedVisitors' in event:
                    print(f"           예상 방문자: {event['expectedVisitors']}명")
                if 'impact' in event:
                    print(f"           영향도: {event['impact']}")
        else:
            print(f"❌ 행사 정보 조회 실패")
    except Exception as e:
        print(f"❌ 에러 발생: {type(e).__name__}: {e}")


def test_different_regions():
    """테스트 3: 지역별 날씨 조회"""
    print("\n=== 테스트 3: 지역별 날씨 조회 ===")
    regions = [
        "서울특별시",
        "부산광역시",
        "대구광역시",
        "인천광역시",
        "광주광역시",
    ]
    
    for region in regions:
        try:
            result = weather(region=region, industry="RETAIL")
            temp = result.get('temperature', 'N/A')
            summary = result.get('summary', 'N/A')
            print(f"✅ {region}: {summary}, {temp}°C")
        except Exception as e:
            print(f"❌ {region}: {type(e).__name__}")


def test_api_response_schema():
    """테스트 4: API 응답 스키마 검증"""
    print("\n=== 테스트 4: API 응답 스키마 검증 ===")
    
    # 날씨 응답 검증
    weather_result = weather(region="서울", industry="CAFE")
    required_weather_fields = [
        "fetchedAt", "location", "summary", "temperature", 
        "humidity", "pop", "confidence", "source"
    ]
    
    print("날씨 API 응답:")
    missing_fields = [f for f in required_weather_fields if f not in weather_result]
    if not missing_fields:
        print(f"✅ 모든 필수 필드 포함: {', '.join(required_weather_fields)}")
    else:
        print(f"❌ 누락된 필드: {', '.join(missing_fields)}")
    
    # 행사 응답 검증
    events_result = events(days=7, region="서울", industry="CAFE")
    required_events_fields = ["events", "location", "fetchedAt", "source", "eventCount"]
    
    print("\n행사 API 응답:")
    missing_fields = [f for f in required_events_fields if f not in events_result]
    if not missing_fields:
        print(f"✅ 모든 필수 필드 포함: {', '.join(required_events_fields)}")
    else:
        print(f"❌ 누락된 필드: {', '.join(missing_fields)}")
    
    # 행사 아이템 스키마 검증
    if events_result.get("events"):
        event = events_result["events"][0]
        required_event_fields = ["name", "date", "location", "category"]
        missing_fields = [f for f in required_event_fields if f not in event]
        if not missing_fields:
            print(f"✅ 행사 아이템 필수 필드 포함")
        else:
            print(f"⚠️  행사 아이템 필드: {missing_fields}")


def test_with_mock_openweathermap():
    """테스트 5: OpenWeatherMap API 시뮬레이션"""
    print("\n=== 테스트 5: OpenWeatherMap API 시뮬레이션 ===")
    
    mock_response_data = {
        "weather": [{"main": "Clear", "description": "맑음"}],
        "main": {
            "temp": 25.5,
            "feels_like": 26.0,
            "humidity": 45,
            "pressure": 1013,
        },
        "wind": {"speed": 3.5},
        "clouds": {"all": 20},
    }
    
    # settings 객체에 API 키 설정
    with patch("src.mcp_service.main.settings") as mock_settings:
        mock_settings.weather_api_key = "test_api_key"
        mock_settings.request_timeout_seconds = 10
        
        with patch("src.mcp_service.main.requests.get") as mock_get:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = mock_response_data
            mock_get.return_value = mock_response
            
            from src.mcp_service.main import _fetch_openweathermap
            
            result = _fetch_openweathermap(37.5665, 126.9780)
            
            if result:
                print(f"✅ OpenWeatherMap 시뮬레이션 성공")
                print(f"   온도: {result['temperature']}°C")
                print(f"   요약: {result['summary']}")
                print(f"   습도: {result['humidity']}%")
                print(f"   풍속: {result['wind_speed']} m/s")
            else:
                print(f"❌ OpenWeatherMap 시뮬레이션 실패")


if __name__ == "__main__":
    print("=" * 70)
    print("MCP Service 실제 API 통합 테스트")
    print("=" * 70)
    
    test_weather_with_mock()
    test_events()
    test_different_regions()
    test_api_response_schema()
    test_with_mock_openweathermap()
    
    print("\n" + "=" * 70)
    print("테스트 완료")
    print("=" * 70)
    print("\n📝 주의사항:")
    print("1. WEATHER_PROVIDER를 'openweathermap'로 설정하려면 WEATHER_API_KEY 필요")
    print("   - OpenWeatherMap 가입: https://openweathermap.org/api")
    print("   - 무료 계획(5 day forecast): https://openweathermap.org/forecast5")
    print("\n2. EVENT_PROVIDER='public_data' 현재는 Mock 데이터 반환")
    print("   - 실제 공공데이터포털 API 통합 필요")
    print("   - 공공데이터포털: https://www.data.go.kr/")
    print("\n3. .env 파일 예제: .env.example 참조")
