# gbridge 개발 계획서

**작성일:** 2026-03-26
**목표:** Tailscale을 통해 모바일 브라우저에서 Gemini CLI를 사용할 수 있는 웹 UI

---

## 개요

M4mini에서 실행 중인 Gemini CLI를 외부(모바일 등)에서 브라우저로 접근해 사용한다.
Tailscale VPN으로 보안 접속, FastAPI 웹서버가 Gemini CLI를 subprocess로 실행하고 WebSocket으로 스트리밍 응답을 전달한다.

---

## 기술 스택

| 레이어 | 기술 |
|--------|------|
| Backend | Python 3, FastAPI, WebSocket |
| Frontend | HTML/CSS/JS (빌드 없음), marked.js CDN |
| Gemini CLI | `gemini -p [prompt] --resume latest` |
| 접속 | Tailscale VPN → `http://[tailscale-ip]:8765` |

---

## 주요 기능

1. **디렉토리 브라우저** — 실행할 작업 디렉토리를 모달 UI로 선택
2. **세션 모드 선택** — 새 세션 / Continue(`--resume latest`) 선택
3. **스트리밍 응답** — WebSocket으로 Gemini 응답 실시간 수신
4. **마크다운 렌더링** — Gemini 응답을 marked.js로 렌더링
5. **모바일 최적화** — 키보드 올라올 때 입력창 유지, safe-area 대응

---

## UI 레이아웃

```
┌──────────────────────────────┐
│  gbridge                     │  ← 헤더 (상태 표시)
├──────────────────────────────┤
│ 📁 /path/here      [변경]   │  ← 현재 디렉토리
│ ○ 새 세션  ● Continue       │  ← 세션 모드
├──────────────────────────────┤
│                              │
│   메시지 영역 (스크롤)        │  ← 유저 오른쪽, Gemini 왼쪽
│   (마크다운 렌더링)           │
│                              │
├──────────────────────────────┤
│  [입력창]          [전송]    │  ← 하단 고정
└──────────────────────────────┘
```

---

## 프로젝트 구조

```
gbridge/
├── backend/
│   └── main.py         ← FastAPI 앱 (WebSocket + 디렉토리 API)
├── frontend/
│   └── index.html      ← 단일 파일 UI
├── docs/
│   └── plan.md         ← 이 파일
├── run.sh              ← venv 생성 + 서버 실행
├── requirements.txt
└── .gitignore
```

---

## WebSocket 프로토콜

```
클라이언트 → 서버 (첫 메시지):
{ "workdir": "/path", "resume": true/false, "prompt": "질문 내용" }

서버 → 클라이언트 (스트리밍):
텍스트 라인 여러 번 전송
...
"\x00DONE"  ← 종료 신호
```

---

## 개발 단계

- [x] 프로젝트 구조 생성
- [x] 백엔드 구현 (FastAPI + WebSocket + 디렉토리 API)
- [x] 프론트엔드 UI 구현
- [ ] 백엔드 동작 테스트 (Gemini CLI subprocess 확인)
- [ ] 프론트엔드 UI 테스트 (로컬)
- [ ] Tailscale 환경에서 모바일 접속 테스트 (사용자 직접)
- [ ] 개선사항 반영

---

## 실행 방법

```bash
cd gbridge
./run.sh
# → http://0.0.0.0:8765
# Tailscale: http://[M4mini tailscale IP]:8765
```

---

## 향후 개선 고려사항

- 기본 인증 (비밀번호) 추가
- 대화 히스토리 저장/불러오기
- 여러 세션 관리
- Gemini 모델 선택 옵션
