# gbridge PTY 세션 방식 전환 계획

**작성일:** 2026-03-26

---

## 목표

세션당 Gemini CLI 프로세스 1개를 PTY로 영구 실행하고,
여러 브라우저가 동일한 입/출력을 실시간 공유한다.

---

## 현재 vs 목표 구조

### 현재 (subprocess -p 방식)
```
메시지 전송 → gemini -p "내용" 실행 → 응답 → 프로세스 종료
메시지 전송 → gemini -p "내용" 실행 → 응답 → 프로세스 종료
```
- 메시지마다 새 프로세스
- Gemini TUI가 아닌 headless 모드

### 목표 (PTY 방식)
```
세션 시작 → gemini (interactive) PTY로 실행 → 계속 살아있음
브라우저 A, B, C → 같은 세션에 연결
입력(어느 브라우저든) → PTY stdin
출력 → 모든 브라우저에 실시간 방송
```

---

## 기술 구조

### PTY 기반 프로세스 실행 (macOS/Linux 공통)

```python
import pty, os, asyncio

master_fd, slave_fd = pty.openpty()
proc = subprocess.Popen(
    ["gemini"],
    stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
    cwd=workdir, close_fds=True
)
os.close(slave_fd)  # 부모는 master만 사용
```

- `master_fd`로 읽기/쓰기
- PTY가 ANSI 제어 문자 포함 전체 출력을 그대로 전달

### 비동기 출력 읽기

PTY `read`는 블로킹 → `run_in_executor`로 별도 스레드에서 처리:

```python
loop.run_in_executor(None, os.read, master_fd, 4096)
```

읽은 데이터 → ANSI 코드 제거 → 모든 WebSocket 클라이언트에 broadcast

### 입력 처리 (큐 방식)

여러 브라우저가 동시 입력 → `asyncio.Queue`에 순서대로 쌓임:

```
브라우저 A: "질문1" ─┐
브라우저 B: "질문2" ──┤→ Queue → 순서대로 PTY stdin에 write
브라우저 C: "질문3" ─┘
```

---

## 응답 경계 감지

Gemini CLI는 동적 TUI (제어 문자 사용). 응답 끝 감지:

| 방법 | 설명 |
|------|------|
| **Idle timeout** | 마지막 출력으로부터 N ms 이내에 추가 출력 없으면 "완료"로 간주 |
| **프롬프트 패턴** | 특정 ANSI 시퀀스나 `>` 패턴 감지 (Gemini CLI 버전 의존) |

→ **Idle timeout (800ms)** 기본 채택, 프롬프트 패턴 보완

---

## Session 객체 변경 사항

```python
class Session:
    id: str
    workdir: str
    master_fd: int          # PTY master fd
    proc: subprocess        # Gemini 프로세스
    clients: List[WebSocket]
    history: List[dict]     # 화면 재생용
    input_queue: asyncio.Queue  # 입력 큐
    state: str              # "idle" | "processing"
    reader_task: Task       # PTY 읽기 백그라운드 태스크
    worker_task: Task       # 입력 큐 처리 태스크
```

---

## WebSocket 메시지 프로토콜 (변경 없음)

```
서버 → 클라이언트:
{ role: "user",         content: "..." }   ← 입력 에코 (히스토리 재생용)
{ role: "gemini_start" }                   ← 응답 시작
{ role: "gemini_chunk", content: "..." }   ← 스트리밍 청크
{ role: "gemini_done" }                    ← 응답 완료 (idle 감지)
{ role: "error",        content: "..." }   ← 에러

클라이언트 → 서버:
{ prompt: "질문 내용" }
```

---

## 프론트엔드 변경 사항

- WebSocket 프로토콜 동일 → **변경 없음**
- 추가 고려: 입력 큐에 쌓인 경우 전송 버튼 비활성화 (처리 중 표시)

---

## 구현 단계

- [ ] 1. `Session` 클래스 리팩터 (PTY fd, 큐, 태스크 추가)
- [ ] 2. `start_session()` → PTY 프로세스 시작 함수
- [ ] 3. PTY 출력 reader 태스크 구현 (ANSI 스트립 + broadcast)
- [ ] 4. 입력 큐 worker 태스크 구현
- [ ] 5. `POST /api/sessions` → PTY 세션 시작으로 변경
- [ ] 6. `DELETE /api/sessions/{id}` → PTY 프로세스 종료
- [ ] 7. 통합 테스트 (Playwright)
- [ ] 8. 에지 케이스: 프로세스 죽음, 브라우저 끊김, 큐 정리

---

## 리스크 및 주의사항

| 리스크 | 대응 |
|--------|------|
| Gemini TUI 출력이 채팅 UI와 맞지 않을 수 있음 | ANSI 완전 제거, 텍스트만 추출 |
| PTY 읽기 중 프로세스 종료 → OSError | try/except + 세션 정리 |
| Idle timeout이 너무 짧으면 응답 잘림 | 조정 가능한 상수로 관리 |
| 동시 입력 시 순서 보장 | asyncio.Queue로 해결 |
| macOS vs Linux PTY 차이 | `pty.openpty()` 표준 API 사용으로 호환 |

---

## 향후 고려사항 (이번 범위 밖)

- 세션 persistent 저장 (서버 재시작 후 복구)
- 입력 중인 클라이언트 표시 ("A가 입력 중...")
- Gemini 모델 선택
- 기본 인증 추가
