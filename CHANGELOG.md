# CHANGELOG

---

## [미출시] — 2026-03-26

### 추가
- 세션 이름 지정 (새 세션 모달)
- 모델 선택 드롭다운 (Gemini 2.5 Pro, 2.0 Flash 등)
- 대화 지속성: `sessions/*.json` 저장, 서버 재시작 후 복구
- `PATCH /api/sessions/{sid}` 세션 이름 변경 API
- `GET /api/sessions/{sid}/export` 대화 마크다운 내보내기
- 채팅 헤더에 내보내기 버튼 (↓ 아이콘)
- 즐겨찾기 UI: 브라우저 모달에서 ★ 버튼으로 추가
- 즐겨찾기 항목 ✕ 버튼으로 삭제
- 응답 완료 시 소리(Web Audio) + 진동(모바일)

### 변경
- 즐겨찾기 하드코딩 → `favorites.json` + `/api/favorites` API
- `GET /api/favorites`, `PUT /api/favorites` 엔드포인트

---

## 2026-03-26 — PTY 방식 전환

### 변경 (핵심)
- Gemini CLI 연동 방식 전면 교체: 영구 PTY 세션 → 메시지마다 `gemini -p` 실행
- `stderr=DEVNULL` 으로 auth/retry 노이즈 제거
- `IDLE_TIMEOUT` 제거 (프로세스 종료 = 응답 완료)
- `_ready_event`, `last_prompt` 제거

### 추가
- `docs/adr/001-per-message-gemini-p.md`
- `docs/adr/002-pty-stdout-devnull-stdin.md`
- `docs/lessons_learned.md`

---

## 2026-03-26 — 초기 구현

### 추가
- FastAPI + WebSocket 백엔드
- 세션 목록 / 채팅 UI
- 디렉토리 브라우저 모달
- ANSI / TUI 노이즈 필터링
- 응답 마크다운 렌더링
- 복사 버튼 (HTTP 환경 fallback 포함)
- 전체화면 토글
