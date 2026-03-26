# gbridge API 문서

**버전:** 2026-03-26

---

## REST API

### 세션

#### `GET /api/sessions`
모든 세션 목록 반환.

**응답:**
```json
[
  {
    "id": "abc12345",
    "name": "내 프로젝트",
    "workdir": "/Users/juitem/M4mini",
    "model": "gemini-2.5-pro-preview-03-25",
    "created_at": "2026-03-26T10:00:00",
    "message_count": 3,
    "state": "idle",
    "alive": true
  }
]
```

---

#### `POST /api/sessions`
새 세션 생성.

**요청:**
```json
{
  "workdir": "/Users/juitem/M4mini",
  "name": "내 프로젝트",
  "model": "gemini-2.0-flash",
  "resume": false
}
```
- `workdir`: 필수. 유효한 디렉토리 경로.
- `name`: 선택. 세션 표시 이름.
- `model`: 선택. 빈 문자열이면 Gemini CLI 기본 모델 사용.
- `resume`: 선택 (현재 미사용 — `--resume latest` 는 첫 메시지 이후 자동 적용).

**응답:** `{ "id": "abc12345" }`

---

#### `PATCH /api/sessions/{sid}`
세션 이름 변경.

**요청:** `{ "name": "새 이름" }`
**응답:** 업데이트된 세션 객체

---

#### `DELETE /api/sessions/{sid}`
세션 삭제 (파일도 함께 삭제).

**응답:** `{ "ok": true }`

---

#### `GET /api/sessions/{sid}/export`
세션 대화를 마크다운 파일로 다운로드.

**응답:** `Content-Type: text/markdown` 파일 (`session-{sid}.md`)

---

### 디렉토리 브라우저

#### `GET /api/browse?path=/some/path`
지정된 경로의 하위 디렉토리 목록 반환 (숨김 폴더 제외).

**응답:**
```json
{
  "current": "/Users/juitem",
  "parent": "/Users",
  "entries": [
    { "name": "M4mini", "path": "/Users/juitem/M4mini" }
  ]
}
```

---

### 즐겨찾기

#### `GET /api/favorites`
`favorites.json` 읽어서 반환.

**응답:**
```json
[
  { "name": "M4mini", "path": "/Users/juitem/M4mini" }
]
```

#### `PUT /api/favorites`
즐겨찾기 전체 교체 저장.

**요청:** 위와 동일한 배열
**응답:** `{ "ok": true }`

---

## WebSocket 프로토콜

**접속:** `ws://{host}/ws/session/{sid}`

접속 시 해당 세션의 전체 대화 히스토리가 즉시 재생됩니다.

### 클라이언트 → 서버

```json
{ "prompt": "질문 내용" }
```

### 서버 → 클라이언트

| 메시지 | 설명 |
|--------|------|
| `{ "role": "user", "content": "..." }` | 입력 에코 (히스토리 재생 포함) |
| `{ "role": "gemini_start" }` | Gemini 응답 시작 |
| `{ "role": "gemini_chunk", "content": "..." }` | 스트리밍 청크 |
| `{ "role": "gemini_done" }` | 응답 완료 |
| `{ "role": "error", "content": "..." }` | 오류 |

---

## 세션 영속성

세션은 `sessions/{id}.json` 에 저장됩니다.
서버 재시작 시 startup 이벤트에서 자동으로 로드합니다.

저장 시점:
- 세션 생성 직후 (히스토리 없는 상태)
- Gemini 응답 완료 후 (각 exchange 후)
- 세션 이름 변경 후

삭제 시: `DELETE /api/sessions/{sid}` 호출 시 파일도 함께 삭제.
