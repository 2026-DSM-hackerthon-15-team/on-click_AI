# ON:CLICK AI PoC

소상공인의 POS 매출과 공공 데이터 도구를 결합해 채팅, 매출 컨설팅, 마감 매출 및 방문자 예측을 제공하는 FastAPI 기반 PoC입니다. Notion의 ON:CLICK 기능/API 명세에 맞춰 내부 API 인증, 공통 오류 DTO, POS 페이지 조회, 구조화 컨설팅 응답을 구현합니다.

## 서비스

| 포트 | 서비스 | 주요 역할 |
|---|---|---|
| 8000 | API Gateway | 매장 소유권, POS 원장, 대시보드, 채팅 내역, AI 프록시 |
| 8001 | AI Service | 읽기 전용 도구형 채팅, 일일 컨설팅 보고서, Instagram 게시 |
| 8002 | MCP Service | 엄격한 JSON 형태의 날씨·주변 행사 데이터 |
| 8003 | Stats Service | POS 거래 기반 마감 매출·내일 방문자 예측 |

## 구현된 흐름

- `POST /ai/chat`: 질문에 대한 답변만 반환합니다. MCP·통계 Tool은 읽기 전용으로 사용할 수 있지만 컨설팅 생성·저장이나 마케팅 게시 같은 부수 효과는 실행하지 않습니다.
- `POST /ai/consultings/daily`: 대상 날짜의 채팅 내역, 매장 지역·업종, POS 매출, 날씨·행사 MCP Tool, 마감 매출·방문자 예측을 종합해 `DAILY_V1` 고정 형식 보고서를 반환합니다. 저장은 메인 백엔드 책임입니다.
- `POST /ai/marketings/{marketingId}/publish/instagram`: 사용자가 승인한 본문·해시태그·HTTPS 이미지 스냅샷을 변경 없이 게시합니다. `FEED`는 이미지 1개, `CAROUSEL`은 2~10개이며 멱등 키로 중복 요청을 막습니다.
- `GET /stores/{storeId}/sales/transactions`: 정상·취소 POS 원장을 페이지/안정 정렬 규약에 맞춰 반환합니다.
- `GET /stores/{storeId}/dashboard/*`: 완료 거래만 사용해 오늘 집계, 24시간 버킷, 마감 매출, 내일 방문자를 계산합니다.
- `POST /ai/consultings`: 기존 기간 비교용 구조화 컨설팅 API이며, 새 일일 보고서는 `/ai/consultings/daily`를 사용합니다.

로컬 데이터는 최근 6주 POS 거래를 시작 시 생성합니다. 취소 거래도 포함되며 모든 통계에서는 제외됩니다. 실제 메인 백엔드와 연결할 때는 `API_BASE_URL`만 변경하면 도구와 통계 서비스가 동일 계약으로 동작합니다.

## 실행

```bash
python -m pip install -r requirements.txt
python src/main.py
```

Docker Compose:

```bash
docker compose up --build
```

`.env.example`을 `.env`로 복사해 백엔드 주소, 내부 키와 선택적 LLM 설정을 지정할 수 있습니다. `AI_PROVIDER=anthropic`은 `ANTHROPIC_API_KEY`, `AI_PROVIDER=openai`는 `OPENAI_API_KEY` 또는 공통 `AI_API_KEY`를 사용합니다. LLM 키 없이도 전체 POS/통계/컨설팅 데모가 동작합니다.

Instagram 게시의 기본값은 안전한 로컬 `mock` Provider입니다. 실제 게시 시 `INSTAGRAM_PROVIDER=meta`로 설정하고, 메인 백엔드가 연결된 계정의 토큰을 `X-Instagram-Access-Token` 헤더로 내부 호출에 전달해야 합니다. 토큰은 요청 본문이나 저장소에 넣지 않습니다.

## 호출 예시

규칙 기반 에이전트 또는 설정된 LLM이 매출 분석 도구를 자동 선택합니다.

```bash
curl -X POST http://localhost:8000/ai/chat \
  -H "Content-Type: application/json" \
  -H "X-Internal-Api-Key: secret" \
  -d '{"userId":1,"storeId":10,"chatRoomId":1,"message":"이번 주 매출이 왜 줄었어?","availableTools":["sales_analysis"]}'
```

POS 원장 조회:

```bash
curl "http://localhost:8000/stores/10/sales/transactions?page=0&size=20&sortBy=soldAt&sortDirection=desc" \
  -H "Authorization: Bearer user-1"
```

일일 컨설팅 보고서 생성:

```bash
curl -X POST http://localhost:8000/ai/consultings/daily \
  -H "Content-Type: application/json" \
  -H "X-Internal-Api-Key: secret" \
  -d '{"userId":1,"storeId":10,"targetDate":"2026-07-14","reportFormat":"DAILY_V1"}'
```

승인된 Instagram 게시물 업로드(`mock` Provider 예시):

```bash
curl -X POST http://localhost:8000/ai/marketings/21/publish/instagram \
  -H "Content-Type: application/json" \
  -H "X-Internal-Api-Key: secret" \
  -d '{"userId":1,"instagramAccountId":"17841400000000000","content":"오늘의 신메뉴를 만나보세요!","hashtags":["#온클릭"],"imageUrls":["https://cdn.example.com/approved.jpg"],"publishType":"FEED","idempotencyKey":"marketing-21-instagram-v1"}'
```

마감 매출 예측:

```bash
curl "http://localhost:8000/stores/10/dashboard/closing-sales-forecast" \
  -H "Authorization: Bearer user-1"
```

## 검증

```bash
python -m unittest discover -s tests -v
python -m compileall -q src tests
```

실제 Claude API로 질문 4종과 일일 보고서를 다시 평가하려면 Compose 앱이 실행 중인 상태에서 다음 명령을 사용합니다. 이 평가는 로컬 계약 데이터를 사용하므로 원격 백엔드 JWT 상태와 무관합니다.

```powershell
docker compose exec -T `
  -e PYTHONPATH=/app `
  -e API_BASE_URL=http://127.0.0.1:8000 `
  -e BACKEND_AUTH_TOKEN= `
  app python tests/manual_claude_evaluation.py
```

Swagger에서 직접 Claude 채팅과 보고서 API를 시험하려면 로컬 계약 데이터용 Compose override를 사용합니다.

```powershell
docker compose -f docker-compose.yml -f docker-compose.claude-test.yml up -d --force-recreate
```

이후 `http://localhost:8000/docs`에서 `userId=1`, `storeId=10`, `chatRoomId=1`, `X-Internal-Api-Key`는 `.env`의 값을 사용합니다. 원격 백엔드 설정으로 돌아갈 때는 `docker compose up -d --force-recreate`를 실행합니다.

테스트는 공통 오류 계약, 매장 격리, POS 페이지 조회, 24개 버킷, 취소 거래 제외, 요일 가중 예측, 채팅의 부수 효과 차단, 일일 보고서 고정 형식, Instagram 입력·멱등성·Meta 호출 순서를 검증합니다.
