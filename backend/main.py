import asyncio
import os
import pty
import select
import re
import shutil
import subprocess
import json
import uuid
from datetime import datetime
from typing import Dict, List, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
import uvicorn

app = FastAPI()

# ── ANSI / TUI output cleaning ───────────────────────────────────────────────
# OSC sequences MUST come before the single-char Fe pattern to avoid partial match
ANSI_RE = re.compile(
    r'\x1B'
    r'(?:'
    r'\][^\x07\x1B]*(?:\x07|\x1B\\)?'  # OSC (e.g. \x1b]0;title\x07)
    r'|[PX^_][^\x1B]*(?:\x1B\\)?'      # DCS/SOS/PM/APC
    r'|\[[0-?]*[ -/]*[@-~]'            # CSI sequences (colors, cursor, etc.)
    r'|[@-Z\\-_]'                       # Fe single-char sequences
    r')'
)
# Positive filter: a line is meaningful if it contains 2+ consecutive
# letters/digits (ASCII or Korean) — i.e. actual words/content
MEANINGFUL_RE = re.compile(r'[a-zA-Z가-힣\d]{2,}')
# Also keep markdown structure lines
MARKDOWN_RE = re.compile(r'^[\s]*(?:#{1,6}\s|[-*+]\s|>\s|`{3}|\d+\.\s|---+|===+)')
# Gemini CLI status bar / TUI chrome lines to always exclude
GEMINI_UI_RE = re.compile(
    r'^\s*(?:'
    r'\?.*for'                      # "? for shortcuts"
    r'|~/'                           # shell path lines (~/...)
    r'|workspace\s*[\(/]'           # workspace status bar
    r'|Ctrl\+[A-Z]'                 # keyboard shortcut hints
    r'|Shift\+Tab'
    r'|Type your message'
    r'|no sandbox'
    r'|Signed in with'
    r'|Plan:\s*Gemini'
    r'|Gemini CLI v'
    r'|Installed with npm'
    r'|update available'
    r'|Waiting for auth'            # startup spinner
    r'|cancel\)'                    # "(Press Esc or Ctrl+C to cancel)"
    r"|We.re making changes"        # policy notice
    r"|What.s Changing"
    r'|policy.violating'
    r'|prioritize traffic'
    r'|capacity.related'
    r'|Read more:'
    r'|geminicli-updates'
    r'|affects you'
    r'|high traffic'
    r'|workflow\.'
    r')',
    re.IGNORECASE
)
# Initialization / retry noise from headless mode stderr (merged into stdout via PTY)
INIT_NOISE_RE = re.compile(
    r'(?:Loaded cached credentials|Attempt \d+ failed|Retrying with backoff|GaxiosError)',
    re.IGNORECASE
)


def clean_output(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    # 1. Strip ANSI escape sequences
    text = ANSI_RE.sub('', text)
    # 1b. Strip incomplete CSI sequences cut off at chunk boundary (e.g. \x1b[38;2;25)
    text = re.sub(r'\x1b\[[0-9;]*', '', text)
    # 1c. Strip box-drawing chars Gemini CLI uses to frame responses
    text = re.sub(r'[│┤╢╖╕╣║╗╝╜╛┐└┴┬├─┼╞╟╚╔╩╦╠═╬╧╨╤╥╙╘╒╓╫╪┘┌╡]', '', text)
    # 2. Handle carriage returns: keep last segment per line
    lines_cr = []
    for line in text.split('\n'):
        parts = line.split('\r')
        lines_cr.append(parts[-1])
    # 3. Positive filter: keep only meaningful lines
    filtered = []
    for line in lines_cr:
        stripped = line.strip()
        if not stripped:
            filtered.append('')  # preserve blank lines for paragraph spacing
        elif INIT_NOISE_RE.search(stripped):
            pass  # discard init/retry messages
        elif (MEANINGFUL_RE.search(stripped) or MARKDOWN_RE.match(stripped)) \
                and not GEMINI_UI_RE.match(stripped) \
                and 'no sandbox' not in stripped.lower():
            filtered.append(line)
        # else: discard TUI noise
    # 4. Collapse 3+ consecutive blank lines into 1
    result = re.sub(r'\n{3,}', '\n\n', '\n'.join(filtered))
    return result


# ── Session ──────────────────────────────────────────────────────────────────
class Session:
    def __init__(self, sid: str, workdir: str):
        self.id = sid
        self.workdir = workdir
        self.created_at = datetime.now().isoformat()
        self.clients: List[WebSocket] = []
        # History stores complete exchanges for replay:
        # {role:"user", content:"..."} or {role:"gemini", content:"..."}
        self.history: List[dict] = []

        self.state: str = "idle"
        self.input_queue: asyncio.Queue = asyncio.Queue()
        self.worker_task: Optional[asyncio.Task] = None
        self._has_session: bool = False      # True after first message succeeds
        self._current_proc: Optional[subprocess.Popen] = None
        self._current_master_fd: Optional[int] = None

    def to_dict(self):
        return {
            "id": self.id,
            "workdir": self.workdir,
            "created_at": self.created_at,
            "message_count": sum(1 for m in self.history if m["role"] == "user"),
            "state": self.state,
            "alive": True,
        }

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self):
        loop = asyncio.get_event_loop()
        self.worker_task = loop.create_task(self._worker_loop())

    async def stop(self):
        if self.worker_task:
            self.worker_task.cancel()
            try:
                await self.worker_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._current_proc and self._current_proc.poll() is None:
            self._current_proc.terminate()
        if self._current_master_fd is not None:
            try:
                os.close(self._current_master_fd)
            except OSError:
                pass

    # ── Broadcasting ──────────────────────────────────────────────────────────

    async def broadcast(self, msg: dict):
        text = json.dumps(msg, ensure_ascii=False)
        dead = []
        for ws in self.clients:
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in self.clients:
                self.clients.remove(ws)

    async def replay_to(self, ws: WebSocket):
        """Send full conversation history to a newly joined client."""
        for item in self.history:
            # Replay as start/chunk/done sequence for gemini messages
            if item["role"] == "gemini":
                for msg in [
                    {"role": "gemini_start"},
                    {"role": "gemini_chunk", "content": item["content"]},
                    {"role": "gemini_done"},
                ]:
                    await ws.send_text(json.dumps(msg, ensure_ascii=False))
            else:
                await ws.send_text(json.dumps(item, ensure_ascii=False))

    # ── Per-message Gemini runner ─────────────────────────────────────────────

    async def _run_gemini(self, prompt: str) -> str:
        """Run `gemini -p <prompt> [--resume latest]`, stream output to clients,
        return the complete response text."""
        gemini_path = shutil.which("gemini") or "gemini"
        cmd = [gemini_path, "-p", prompt]
        if self._has_session:
            cmd += ["--resume", "latest"]

        master_fd, slave_fd = pty.openpty()
        self._current_master_fd = master_fd

        loop = asyncio.get_event_loop()

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=slave_fd,
            stderr=subprocess.DEVNULL,  # discard init/retry noise
            cwd=self.workdir,
            close_fds=True,
        )
        self._current_proc = proc
        os.close(slave_fd)

        response_buf = ""

        def _read():
            """Blocking read with 100ms select timeout."""
            try:
                r, _, _ = select.select([master_fd], [], [], 0.1)
                if r:
                    return os.read(master_fd, 4096)
                return b""
            except OSError:
                return None  # fd closed / process dead

        while True:
            raw = await loop.run_in_executor(None, _read)
            if raw is None:
                break  # PTY closed
            if raw:
                text = clean_output(raw)
                if text.strip():
                    await self.broadcast({"role": "gemini_chunk", "content": text})
                    response_buf += text
            elif proc.poll() is not None:
                # No data and process has exited — drain once more then stop
                raw2 = await loop.run_in_executor(None, _read)
                if raw2:
                    text = clean_output(raw2)
                    if text.strip():
                        await self.broadcast({"role": "gemini_chunk", "content": text})
                        response_buf += text
                break

        try:
            os.close(master_fd)
        except OSError:
            pass
        self._current_master_fd = None
        self._current_proc = None
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()

        return response_buf

    # ── Input worker ──────────────────────────────────────────────────────────

    async def _worker_loop(self):
        while True:
            try:
                prompt = await self.input_queue.get()

                # Record user message and broadcast
                msg = {"role": "user", "content": prompt}
                self.history.append(msg)
                await self.broadcast(msg)

                self.state = "processing"
                await self.broadcast({"role": "gemini_start"})

                response = await self._run_gemini(prompt)

                self._has_session = True
                self.history.append({"role": "gemini", "content": response})
                self.state = "idle"
                await self.broadcast({"role": "gemini_done"})

                self.input_queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception:
                self.state = "idle"
                await self.broadcast({"role": "error", "content": "Gemini 처리 중 오류가 발생했습니다."})
                try:
                    self.input_queue.task_done()
                except Exception:
                    pass


# ── Global session store ─────────────────────────────────────────────────────
sessions: Dict[str, Session] = {}


# ── REST API ─────────────────────────────────────────────────────────────────

@app.get("/api/sessions")
def list_sessions():
    return [s.to_dict() for s in sessions.values()]


@app.post("/api/sessions")
async def create_session(body: dict):
    sid = str(uuid.uuid4())[:8]
    workdir = body.get("workdir", os.path.expanduser("~"))
    if not os.path.isdir(workdir):
        return JSONResponse({"error": "Invalid workdir"}, status_code=400)
    s = Session(sid, workdir)
    sessions[sid] = s
    await s.start()
    return {"id": sid}


@app.delete("/api/sessions/{sid}")
async def delete_session(sid: str):
    if sid not in sessions:
        return JSONResponse({"error": "Not found"}, status_code=404)
    await sessions[sid].stop()
    del sessions[sid]
    return {"ok": True}


@app.get("/api/browse")
def browse(path: str = "/"):
    try:
        path = os.path.expanduser(path)
        if not os.path.isdir(path):
            return JSONResponse({"error": "Not a directory"}, status_code=400)
        entries = sorted(
            [{"name": n, "path": os.path.join(path, n)}
             for n in os.listdir(path)
             if os.path.isdir(os.path.join(path, n)) and not n.startswith(".")],
            key=lambda e: e["name"]
        )
        parent = str(os.path.dirname(path)) if path != "/" else None
        return {"current": path, "parent": parent, "entries": entries}
    except PermissionError:
        return JSONResponse({"error": "Permission denied"}, status_code=403)


# ── WebSocket ────────────────────────────────────────────────────────────────

@app.websocket("/ws/session/{sid}")
async def session_ws(websocket: WebSocket, sid: str):
    if sid not in sessions:
        await websocket.close(code=4004)
        return

    session = sessions[sid]
    await websocket.accept()
    session.clients.append(websocket)

    # Replay history
    await session.replay_to(websocket)

    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            prompt = data.get("prompt", "").strip()
            if prompt:
                await session.input_queue.put(prompt)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if websocket in session.clients:
            session.clients.remove(websocket)


# ── Static frontend ──────────────────────────────────────────────────────────
app.mount("/", StaticFiles(directory="frontend", html=True), name="static")

if __name__ == "__main__":
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8765, reload=True)
