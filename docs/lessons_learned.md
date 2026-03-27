# gbridge 개발 교훈 (Lessons Learned)

**작성일:** 2026-03-26
**목적:** 다음 개발 시 같은 실수 반복 방지

---

## 1. PTY echo 필터 — 두 곳 모두 수정해야 함

**문제:** PTY는 사용자 입력을 그대로 echo한다. 이게 Gemini 응답처럼 보여서 화면에 출력됨.

**해결:**
- `Session.__init__`에 `self.last_prompt: str = ""` 추가
- `_worker_loop`에서 PTY write **직전**에 `self.last_prompt = prompt` 저장
- `_reader_loop`에서 출력 텍스트에 `last_prompt` 포함 시 제거

```python
# _worker_loop
self.last_prompt = prompt          # ← 반드시 write 전에
os.write(self.master_fd, (prompt + "\n").encode())

# _reader_loop
if self.last_prompt and self.last_prompt in text:
    text = text.replace(self.last_prompt, '').strip()
```

**주의:** 두 곳 중 하나라도 빠지면 동작 안 함.

---

## 2. IDLE_TIMEOUT — Gemini API 응답 대기 시간 충분히 확보

**문제:** Gemini CLI는 PTY echo를 즉시 내보내고, 실제 AI 응답은 3~10초 후 도착.
`IDLE_TIMEOUT = 0.8`이면 echo 후 0.8초 만에 "완료" 선언 → 실제 응답 무시.

**해결:** `IDLE_TIMEOUT = 8.0` (최소 8초)

```python
IDLE_TIMEOUT = 8.0  # Gemini API 레이턴시 커버
```

**주의:** 너무 짧으면 응답이 잘림. 네트워크 상태 나쁠 때는 더 길게 설정.

---

## 3. ANSI 정규식 — OSC 패턴 순서 중요

**문제:** `[@-Z\\-_]` 패턴이 `]` 문자를 먼저 소비해 OSC 시퀀스(`\x1b]...`)가 제대로 안 걷힘.

**해결:** OSC/DCS 패턴을 **Fe single-char 패턴보다 먼저** 배치

```python
ANSI_RE = re.compile(
    r'\x1B'
    r'(?:'
    r'\][^\x07\x1B]*(?:\x07|\x1B\\)?'  # OSC — 먼저
    r'|[PX^_][^\x1B]*(?:\x1B\\)?'      # DCS/SOS/PM/APC
    r'|\[[0-?]*[ -/]*[@-~]'            # CSI
    r'|[@-Z\\-_]'                       # Fe single-char — 마지막
    r')'
)
```

---

## 4. TUI 노이즈 필터 — positive filter + exclusion list 조합

**문제:** Gemini CLI TUI 크롬 라인들(`? for shortcuts`, `~/path no sandbox`, `Ctrl+X ...`)이
positive filter(3글자 이상 단어 포함)를 통과해버림.

**해결:** `MEANINGFUL_RE` (positive) + `GEMINI_UI_RE` (exclusion) 두 단계 필터

```python
if (MEANINGFUL_RE.search(stripped) or MARKDOWN_RE.match(stripped)) \
        and not GEMINI_UI_RE.match(stripped):
    filtered.append(line)
```

`GEMINI_UI_RE`에 추가할 패턴 목록:
- `? for shortcuts`, `Ctrl+[A-Z]`, `Shift+Tab`
- `~/` (경로 표시줄)
- `workspace (`, `no sandbox`
- `Signed in with`, `Plan: Gemini`, `Gemini CLI v`
- `Installed with npm`, `update available`
- `Type your message`

---

## 5. 프론트엔드 API 필드명 — 백엔드와 정확히 일치시키기

**문제:** `/api/browse` 응답의 필드명이 `entries`인데 프론트엔드가 `data.dirs`로 읽음 → 디렉토리 목록 빈 상태.

**해결:** 백엔드 응답 구조를 먼저 확인 후 프론트엔드 작성

```python
# 백엔드 응답
return {"current": path, "parent": parent, "entries": entries}
# entries 각 항목: {"name": n, "path": full_path}
```

```javascript
// 프론트엔드
const entries = data.entries;  // data.dirs 아님
entry.path                     // entry.name 아님 (full path 사용)
```

---

## 6. HTTP 환경에서 클립보드 복사

**문제:** Tailscale은 HTTP. `navigator.clipboard.writeText()`는 HTTPS/localhost에서만 작동.

**해결:** 세 단계 fallback

```javascript
async function copyText(text) {
    // 1. Modern API (HTTPS/localhost only)
    if (navigator.clipboard && location.protocol === 'https:') {
        await navigator.clipboard.writeText(text);
        return;
    }
    // 2. execCommand fallback (deprecated but works on mobile HTTP)
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;opacity:0';
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    // 3. Last resort
    // window.prompt('복사:', text);
}
```

---

## 7. 백그라운드 서버 충돌

**문제:** 테스트용으로 백그라운드에서 서버를 띄운 뒤 kill을 안 하면, 다음 실행 시 "포트 이미 사용 중" 오류.

**해결:** 서버 재시작 전 항상 기존 프로세스 kill

```bash
pkill -f "backend.main"
sleep 1
python3 -m backend.main &
```

---

## 8. Edit 도구 — old_string 정확히 일치해야 함

**문제:** Edit 도구의 `old_string`이 실제 파일 내용과 공백/들여쓰기 하나라도 다르면 "String not found" 오류.

**해결 순서:**
1. 편집 전 `Grep`으로 해당 라인 번호 확인
2. `Read`로 전후 5줄 맥락 확인
3. `old_string`을 충분히 길게 (앞뒤 맥락 포함) 잡기

---

## 9. 현재 구조 요약 (stream-json 방식)

```
메시지 수신
  └─ gemini -p "prompt" --output-format stream-json [--resume latest]
  └─ PTY stdout → JSON 라인 파싱
  └─ type:message + role:assistant + delta:true → broadcast(gemini_chunk)
  └─ 프로세스 exit → broadcast(gemini_done)
```

---

## 10. persistent session 시도 및 한계

**목표:** Gemini 프로세스를 세션 동안 한 번만 실행해서 파일 컨텍스트 재전송 방지

**시도한 방법들:**
- stdin PIPE (no EOF): Gemini가 EOF 없이는 처리 안 함 (TTY 환경 무관)
- stdin PTY: interactive TUI 모드로 진입 → `--output-format stream-json` 무시됨
- TIOCSCTTY + setsid: 부팅은 되나 터미널 쿼리(`\x1b[c` DA1 등)에 응답해야 하고
  응답해도 결국 TUI 모드로 동작

**핵심 발견:**
- Gemini CLI는 stdin TTY 여부로 모드를 결정: TTY → interactive TUI, pipe → 배치(EOF 필요)
- `--output-format stream-json`은 배치 모드에서만 작동
- **stdin pipe 모드는 EOF를 받아야만 처리 시작** (실제 터미널 환경에서도 동일)
- 진정한 persistent session은 Gemini REST API 직접 호출(OAuth 토큰)로만 가능

**workaround:** `~/M4miniGemini` 같은 경량 작업 디렉토리 사용 → 파일 컨텍스트 최소화
