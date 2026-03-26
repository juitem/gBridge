# gbridge

Gemini CLI를 모바일 브라우저에서 사용할 수 있는 웹 UI.
Tailscale VPN으로 보안 접속, FastAPI + WebSocket으로 실시간 스트리밍.

---

## 요구사항

- Python 3.9+
- [Gemini CLI](https://github.com/google-gemini/gemini-cli) (`npm install -g @google/gemini-cli`)
- Gemini CLI 인증 완료 (`gemini` 명령어 실행 가능한 상태)

---

## 빠른 시작

```bash
git clone https://github.com/juitem/gBridge.git
cd gBridge
./run.sh
```

브라우저에서 `http://localhost:8765` 접속.

Tailscale 환경: `http://[M4mini-tailscale-IP]:8765`

---

## 기능

- **세션 관리** — 세션 이름 지정, 여러 세션 동시 운영
- **대화 지속성** — 서버 재시작 후에도 대화 히스토리 유지 (`sessions/*.json`)
- **모델 선택** — Gemini 2.5 Pro, 2.0 Flash 등 선택 가능
- **디렉토리 브라우저** — 작업 디렉토리를 모달 UI로 선택
- **즐겨찾기** — `favorites.json` 파일 또는 UI에서 추가/삭제
- **마크다운 렌더링** — Gemini 응답 실시간 렌더링
- **대화 내보내기** — `.md` 파일로 다운로드
- **멀티 클라이언트** — 같은 세션을 여러 브라우저에서 동시 접속
- **완료 알림** — 응답 완료 시 소리 + 진동 (모바일)

---

## 즐겨찾기 설정

`favorites.json` 직접 편집:

```json
[
  { "name": "M4mini",   "path": "/Users/yourname/M4mini" },
  { "name": "Projects", "path": "/Users/yourname/Projects" }
]
```

또는 디렉토리 브라우저에서 ★ 버튼으로 추가.

---

## 프로젝트 구조

```
gbridge/
├── backend/
│   └── main.py          # FastAPI 앱
├── frontend/
│   └── index.html       # 단일 파일 UI
├── sessions/            # 세션 히스토리 (자동 생성, git 제외)
├── docs/
│   ├── adr/             # Architecture Decision Records
│   ├── api.md           # API 문서
│   ├── plan.md          # 프로젝트 계획
│   └── lessons_learned.md
├── favorites.json       # 즐겨찾기
├── run.sh
└── requirements.txt
```

---

## 기술 스택

| 레이어 | 기술 |
|--------|------|
| Backend | Python 3, FastAPI, WebSocket |
| Frontend | HTML/CSS/JS (빌드 없음), marked.js CDN |
| Gemini | `gemini -p <prompt> --resume latest` |
| 접속 | Tailscale VPN |
