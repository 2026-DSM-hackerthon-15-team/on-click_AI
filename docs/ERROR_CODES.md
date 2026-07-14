# ON:CLICK AI 연동 오류 코드

모든 Gateway/AI/Stats 오류 응답은 다음 필드를 사용합니다.

```json
{
  "errorCode": "AI_SERVICE_CONNECTION_FAILED",
  "message": "AI_SERVICE에 연결할 수 없습니다.",
  "requestId": "backend-chat-001",
  "retryable": true,
  "details": {
    "upstreamService": "AI_SERVICE",
    "stage": "connect"
  }
}
```

- `requestId`: 백엔드가 `X-Request-ID`로 보낸 값을 그대로 사용합니다. 없으면 AI 서버가 생성합니다.
- `retryable`: 동일 요청을 재시도해도 되는 일시적 오류인지 나타냅니다.
- `details`: 실패한 서비스, 단계, upstream 상태 코드 등 비밀정보가 아닌 진단 정보입니다.
- 비밀번호, JWT, 내부 API Key, LLM API Key, 요청 본문은 응답과 로그에 기록하지 않습니다.

## 요청·인증

| 오류 코드 | HTTP | 재시도 | 의미 |
|---|---:|---:|---|
| `INVALID_INTERNAL_API_KEY` | 401 | 아니요 | 내부 API Key가 없거나 불일치 |
| `UNAUTHORIZED` | 401 | 아니요 | JWT가 없거나 형식이 잘못됨 |
| `STORE_ACCESS_DENIED` | 403 | 아니요 | JWT 사용자가 매장 소유자가 아님 |
| `INVALID_*_REQUEST` | 400 | 아니요 | DTO 필드 누락·형식·범위 오류 |
| `INTERNAL_SERVER_ERROR` | 500 | 아니요 | 처리되지 않은 서버 내부 예외. `requestId`로 로그 확인 |

## 서비스 간 HTTP 연동

`{SERVICE}`는 `AI_SERVICE`, `BACKEND_API`, `MCP_SERVICE`, `STATS_SERVICE` 중 하나입니다.

| 오류 코드 | HTTP | 재시도 | 실패 단계 |
|---|---:|---:|---|
| `{SERVICE}_CONNECT_TIMEOUT` | 504 | 예 | TCP/TLS 연결 시간 초과 |
| `{SERVICE}_CONNECTION_FAILED` | 502 | 예 | 연결 거부, DNS, 네트워크 단절 |
| `{SERVICE}_TIMEOUT` 또는 `AI_TIMEOUT` | 504 | 예 | 연결 후 응답 대기 시간 초과 |
| `{SERVICE}_REQUEST_FAILED` | 502 | 예 | 그 외 HTTP 클라이언트 통신 오류 |
| `{SERVICE}_HTTP_ERROR` | 502 | 상태에 따름 | upstream이 자체 오류 코드를 주지 않고 4xx/5xx 반환 |
| `{SERVICE}_INVALID_JSON_RESPONSE` | 502 | 아니요 | 성공 응답이 JSON이 아님 |

upstream이 `errorCode`를 반환하면 Gateway는 그 코드를 유지하고 `details.upstreamRequestId`를 추가합니다.

## AI 제공자·마케팅 이미지

| 오류 코드 | HTTP | 재시도 | 의미 |
|---|---:|---:|---|
| `AI_PROVIDER_AUTHENTICATION_FAILED` | 502 | 아니요 | Anthropic/OpenAI API Key 또는 권한 오류 |
| `AI_PROVIDER_RATE_LIMITED` | 503 | 예 | 제공자 요청 한도 초과 |
| `AI_PROVIDER_CONNECTION_FAILED` | 502 | 예 | AI 제공자 연결 실패 |
| `AI_PROVIDER_TIMEOUT` | 504 | 예 | AI 제공자 응답 시간 초과 |
| `AI_PROVIDER_REJECTED_REQUEST` | 422 | 아니요 | 제공자가 입력을 거부함 |
| `MARKETING_IMAGE_URL_BLOCKED` | 422 | 아니요 | 사설망·loopback 등 SSRF 위험 주소 |
| `MARKETING_IMAGE_DNS_FAILED` | 502 | 예 | 이미지 호스트 DNS 확인 실패 |
| `MARKETING_IMAGE_CONNECTION_FAILED` | 502 | 예 | 이미지 호스트 연결 실패 |
| `MARKETING_IMAGE_DOWNLOAD_TIMEOUT` | 504 | 예 | 이미지 다운로드 시간 초과 |
| `MARKETING_IMAGE_HTTP_ERROR` | 502 | 예 | 이미지 호스트 HTTP 오류 |
| `MARKETING_IMAGE_TYPE_UNSUPPORTED` | 422 | 아니요 | JPEG/PNG/GIF/WebP 이외의 형식 |
| `MARKETING_IMAGE_TOO_LARGE` | 413 | 아니요 | 파일당 5MB 제한 초과 |

## Browser MCP·Instagram

| 오류 코드 | HTTP | 재시도 | 의미 |
|---|---:|---:|---|
| `INSTAGRAM_CREDENTIALS_INVALID` | 422 | 아니요 | Instagram ID 또는 비밀번호 불일치 |
| `INSTAGRAM_LOGIN_CHALLENGE_REQUIRED` | 422 | 아니요 | 2FA, CAPTCHA, 로그인 확인 필요 |
| `INSTAGRAM_PUBLISH_TIMEOUT` | 504 | 예 | 브라우저 게시 제한 시간 초과 |
| `BROWSER_MCP_UNAVAILABLE` | 502 | `details.reasonCode` 참고 | Browser MCP 게시 실패 |

`BROWSER_MCP_UNAVAILABLE`의 세부 원인은 `details.reasonCode`에 기록됩니다.

- `BROWSER_MCP_NOT_CONFIGURED`
- `BROWSER_MCP_CLIENT_NOT_INSTALLED`
- `BROWSER_MCP_CONNECTION_FAILED`
- `BROWSER_MCP_TOOL_CALL_FAILED`
- `BROWSER_MCP_TOOL_FAILED`
- `BROWSER_MCP_INVALID_RESPONSE`
- `INSTAGRAM_PROVIDER_NOT_SUPPORTED`

## 서버 로그 확인

```bash
docker compose logs -f --tail=200 app
docker compose logs --since=30m app | grep 'backend-chat-001'
docker compose logs --since=30m app | grep 'upstream.request.failed'
```

각 줄은 JSON이며 주요 이벤트는 `request.started`, `request.completed`, `api.error`,
`upstream.request.*`, `tool.upstream.*`, `pos.request.*`, `browser_mcp.request.*`입니다.
