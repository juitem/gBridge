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
# After ANSI strip, catch leftover fragments like "[38;2;25" or "0;◇ Ready"
LEFTOVER_RE = re.compile(r'(?:\d+;)+[^\s\w가-힣]*|^\s*\d+;')

# Positive filter: a line is meaningful if it contains 3+ consecutive
# letters/digits (ASCII or Korean) — i.e. actual words/content
MEANINGFUL_RE = re.compile(r'[a-zA-Z가-힣\d]{3,}')
# Also keep markdown structure lines
MARKDOWN_RE = re.compile(r'^[\s]*(?:#{1,6}\s|[-*+]\s|>\s|`{3}|\d+\.\s|---+|===+)')


def clean_output(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    # 1. Strip ANSI escape sequences
    text = ANSI_RE.sub('', text)
    # 2. Handle carriage returns: keep last segment per line
    lines_cr = []
    for line in text.split('\n'):
        parts = line.split('\r')
        lines_cr.append(parts[-1])
    text = '\n'.join(lines_cr)
    # 3. Positive filter: keep only meaningful lines
    filtered = []
    for line in lines_cr:
        stripped = line.strip()
        if not stripped:
            filtered.append('')  # preserve blank lines for paragraph spacing
        elif MEANINGFUL_RE.search(stripped) or MARKDOWN_RE.match(stripped):
            filtered.append(line)
        # else: discard TUI noise
    # 4. Collapse 3+ consecutive blank lines into 1
    result = re.sub(r'\n{3,}', '\n\n', '\n'.join(filtered))
    return result

# ── Idle timeout: seconds of silence = Gemini done responding ────────────────
IDLE_TIMEOUT = 0.8


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

        self.master_fd: Optional[int] = None
        self.proc: Optional[subprocess.Popen] = None
        self.input_queue: asyncio.Queue = asyncio.Queue()
        self.state: str = "starting"   # starting | idle | processing
        self.reader_task: Optional[asyncio.Task] = None
        self.worker_task: Optional[asyncio.Task] = None

    def to_dict(self):
        alive = self.proc is not None and self.proc.poll() is None
        return {
            "id": self.id,
            "workdir": self.workdir,
            "created_at": self.created_at,
            "message_count": sum(1 for m in self.history if m["role"] == "user"),
            "state": self.state,
            "alive": alive,
        }

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self):
        master_fd, slave_fd = pty.openpty()
        self.master_fd = master_fd

        gemini_path = shutil.which("gemini") or "gemini"
        self.proc = subprocess.Popen(
            [gemini_path],
            stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
            cwd=self.workdir, close_fds=True,
        )
        os.close(slave_fd)

        loop = asyncio.get_event_loop()
        self.reader_task = loop.create_task(self._reader_loop())
        self.worker_task = loop.create_task(self._worker_loop())

    async def stop(self):
        for t in (self.reader_task, self.worker_task):
            if t:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
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

    # ── PTY reader ────────────────────────────────────────────────────────────

    async def _reader_loop(self):
        loop = asyncio.get_event_loop()
        in_response = False
        response_buf = ""
        last_output = None

        def _read():
            """Blocking read with 100ms select timeout."""
            try:
                r, _, _ = select.select([self.master_fd], [], [], 0.1)
                if r:
                    return os.read(self.master_fd, 4096)
                return b""
            except OSError:
                return None  # fd closed / process dead

        while True:
            try:
                raw = await loop.run_in_executor(None, _read)
                if raw is None:
                    break  # process died

                now = loop.time()

                if raw:
                    text = clean_output(raw)
                    if text.strip():
                        if not in_response:
                            in_response = True
                            self.state = "processing"
                            await self.broadcast({"role": "gemini_start"})
                        await self.broadcast({"role": "gemini_chunk", "content": text})
                        response_buf += text
                        last_output = now
                else:
                    # No data this tick — check idle timeout
                    if in_response and last_output and (now - last_output) >= IDLE_TIMEOUT:
                        in_response = False
                        self.state = "idle"
                        # Save complete response to history
                        self.history.append({"role": "gemini", "content": response_buf})
                        response_buf = ""
                        last_output = None
                        await self.broadcast({"role": "gemini_done"})

            except asyncio.CancelledError:
                break
            except Exception:
                break

        # Process exited
        self.state = "idle"
        await self.broadcast({"role": "error", "content": "Gemini 프로세스가 종료되었습니다."})

    # ── Input worker ──────────────────────────────────────────────────────────

    async def _worker_loop(self):
        # Wait for Gemini startup output to settle
        await asyncio.sleep(2)
        self.state = "idle"

        while True:
            try:
                prompt = await self.input_queue.get()

                # Wait until previous response is done
                for _ in range(100):
                    if self.state != "processing":
                        break
                    await asyncio.sleep(0.1)

                # Record user message and broadcast
                msg = {"role": "user", "content": prompt}
                self.history.append(msg)
                await self.broadcast(msg)

                # Send to PTY
                try:
                    os.write(self.master_fd, (prompt + "\n").encode())
                except OSError:
                    pass

                self.input_queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception:
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
