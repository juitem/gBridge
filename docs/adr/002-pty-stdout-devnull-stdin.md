# ADR 002: PTY stdout + DEVNULL stdin 조합

**날짜:** 2026-03-26
**상태:** 채택됨

---

## 컨텍스트

`gemini -p` 방식 채택 후, 프로세스의 stdout/stderr 연결 방법을 결정해야 했다.

### 옵션 검토

| 조합 | stdout | stderr | 결과 |
|------|--------|--------|------|
| A | PIPE | PIPE | Gemini가 stdout=TTY 아님 감지 → 출력 형식 변경 가능성 |
| B | PTY | PTY | stderr의 재시도/인증 로그가 응답에 섞임 |
| C | PTY | DEVNULL | Gemini는 TTY로 인식, stderr 노이즈 없음 ✓ |

## 결정

**C 채택: `stdout=slave_fd` (PTY), `stderr=DEVNULL`**

```python
master_fd, slave_fd = pty.openpty()
proc = subprocess.Popen(
    cmd,
    stdin=subprocess.DEVNULL,
    stdout=slave_fd,
    stderr=subprocess.DEVNULL,
    cwd=workdir,
    close_fds=True,
)
os.close(slave_fd)  # 부모는 master_fd 만 사용
```

## 이유

- `stdout=slave_fd`: Gemini가 출력 스트림을 TTY로 인식 → 정상 동작
- `stderr=DEVNULL`: 429 재시도, "Loaded cached credentials." 등 노이즈 차단
  → 사용자에게 불필요한 기술적 메시지 전달 방지
- `stdin=DEVNULL`: `-p` 플래그 사용 시 stdin 불필요

## 결과

- ANSI 스트립 + 박스 드로잉 문자 제거 로직은 유지 (PTY 출력에 포함될 수 있음)
- `INIT_NOISE_RE` 필터 추가 (혹시 stdout에 섞이는 경우 대비)
- master_fd에서 OSError → 프로세스 종료로 판단하고 루프 탈출
