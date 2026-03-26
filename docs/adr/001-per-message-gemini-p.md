# ADR 001: Per-message `gemini -p` 방식 채택

**날짜:** 2026-03-26
**상태:** 채택됨

---

## 컨텍스트

Gemini CLI를 웹에서 사용하기 위해 두 가지 방식을 검토했다.

### 방식 A: PTY 영구 실행 (최초 설계)
세션당 `gemini` 프로세스를 1개 PTY로 영구 실행하고,
사용자 입력을 PTY stdin에 키 입력으로 전달한다.

**문제점:**
- Gemini CLI 소스 `KeypressContext.js`에 `FAST_RETURN_TIMEOUT = 30ms` 존재
  → Enter 키가 마지막 입력 후 30ms 이내에 오면 shift+Enter로 변환 (줄바꿈 처리)
  → PTY를 통해 prompt+"\n" 을 한 번에 쓰면 30ms 안에 도착 → 제출 불가
- `gemini.js`의 `readStdin()`: stdin이 TTY가 아닐 때 EOF까지 전체를 버퍼링
  → stdin=PIPE + 개방 상태로는 응답을 받을 수 없음

### 방식 B: 메시지마다 새 프로세스 (채택)
메시지마다 `gemini -p "<prompt>"` 를 실행하고 종료를 기다린다.

```
gemini -p "질문 내용"                     # 첫 번째 메시지
gemini -p "질문 내용" --resume latest     # 이후 메시지 (컨텍스트 유지)
```

stdout → PTY slave_fd (ANSI 포함 출력)
stdin → DEVNULL
stderr → DEVNULL (재시도/인증 노이즈 제거)

## 결정

방식 B 채택.

**이유:**
- `-p` 플래그는 Gemini CLI 공식 headless 모드
- 프로세스 종료 = 응답 완료 → IDLE_TIMEOUT 불필요
- `--resume latest` 로 이전 대화 컨텍스트 유지 가능
- stdin 문제, FAST_RETURN_TIMEOUT 문제 모두 회피

## 결과

- `Session` 클래스: `master_fd`, `proc` 영구 보관 제거
- `_reader_loop` 제거 → `_run_gemini()` 메서드로 대체
- `IDLE_TIMEOUT`, `_ready_event`, `last_prompt` 제거
- `stderr=DEVNULL` 으로 auth/retry 노이즈 완전 제거
