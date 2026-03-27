import asyncio
import glob
import os
import pty
import select
import shutil
import subprocess
import json
import uuid
from datetime import datetime
from typing import Dict, List, Optional
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

app = FastAPI()

# ── Paths ─────────────────────────────────────────────────────────────────────
_BASE = os.path.dirname(os.path.abspath(__file__))
SESSIONS_DIR  = os.path.join(_BASE, "..", "sessions")
FAVORITES_FILE = os.path.join(_BASE, "..", "favorites.json")
os.makedirs(SESSIONS_DIR, exist_ok=True)


# ── Session ──────────────────────────────────────────────────────────────────
class Session:
    def __init__(self, sid: str, workdir: str):
        self.id = sid
        self.name: str = ""
        self.workdir = workdir
        self.model: str = ""
        self.created_at = datetime.now().isoformat()
        self.clients: List[WebSocket] = []
        self.history: List[dict] = []

        self.state: str = "idle"
        self.input_queue: asyncio.Queue = asyncio.Queue()
        self.worker_task: Optional[asyncio.Task] = None
        self._has_session: bool = False
        self._current_proc: Optional[subprocess.Popen] = None
        self._current_master_fd: Optional[int] = None

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "workdir": self.workdir,
            "model": self.model,
            "created_at": self.created_at,
            "message_count": sum(1 for m in self.history if m["role"] == "user"),
            "state": self.state,
            "alive": True,
        }

    def save(self):
        data = {
            "id": self.id,
            "name": self.name,
            "workdir": self.workdir,
            "model": self.model,
            "created_at": self.created_at,
            "history": self.history,
        }
        path = os.path.join(SESSIONS_DIR, f"{self.id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

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
        for item in self.history:
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
        gemini_path = shutil.which("gemini") or "gemini"
        cmd = [gemini_path, "-p", prompt, "--output-format", "stream-json"]
        if self.model:
            cmd += ["--model", self.model]
        if self._has_session:
            cmd += ["--resume", "latest"]

        master_fd, slave_fd = pty.openpty()
        self._current_master_fd = master_fd
        loop = asyncio.get_event_loop()

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=slave_fd,
            stderr=subprocess.DEVNULL,
            cwd=self.workdir,
            close_fds=True,
        )
        self._current_proc = proc
        os.close(slave_fd)

        response_buf = ""
        line_buf = ""

        def _read():
            try:
                r, _, _ = select.select([master_fd], [], [], 0.1)
                if r:
                    return os.read(master_fd, 4096)
                return b""
            except OSError:
                return None

        while True:
            raw = await loop.run_in_executor(None, _read)
            if raw is None:
                break
            if raw:
                line_buf += raw.decode("utf-8", errors="replace")
                # stream chunks to client as they arrive
                while "\n" in line_buf:
                    line, line_buf = line_buf.split("\n", 1)
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") == "message" and \
                       event.get("role") == "assistant" and \
                       event.get("delta") and event.get("content"):
                        content = event["content"]
                        await self.broadcast({"role": "gemini_chunk", "content": content})
                        response_buf += content
            elif proc.poll() is not None:
                raw2 = await loop.run_in_executor(None, _read)
                if raw2:
                    line_buf += raw2.decode("utf-8", errors="replace")
                for line in line_buf.split("\n"):
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") == "message" and \
                       event.get("role") == "assistant" and \
                       event.get("delta") and event.get("content"):
                        content = event["content"]
                        await self.broadcast({"role": "gemini_chunk", "content": content})
                        response_buf += content
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

                msg = {"role": "user", "content": prompt}
                self.history.append(msg)
                await self.broadcast(msg)

                self.state = "processing"
                await self.broadcast({"role": "gemini_start"})

                response = await self._run_gemini(prompt)

                self._has_session = True
                self.history.append({"role": "gemini", "content": response})
                self.state = "idle"
                self.save()
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


# ── Startup: load persisted sessions ─────────────────────────────────────────
@app.on_event("startup")
async def load_saved_sessions():
    for fpath in glob.glob(os.path.join(SESSIONS_DIR, "*.json")):
        try:
            with open(fpath, encoding="utf-8") as f:
                data = json.load(f)
            sid = data["id"]
            if sid in sessions:
                continue
            s = Session(sid, data["workdir"])
            s.name       = data.get("name", "")
            s.model      = data.get("model", "")
            s.history    = data.get("history", [])
            s._has_session = len(s.history) > 0
            s.created_at = data.get("created_at", s.created_at)
            sessions[sid] = s
            await s.start()
        except Exception:
            pass


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
    s.name  = body.get("name", "")
    s.model = body.get("model", "")
    sessions[sid] = s
    await s.start()
    s.save()
    return {"id": sid}


@app.patch("/api/sessions/{sid}")
async def update_session(sid: str, req: Request):
    if sid not in sessions:
        return JSONResponse({"error": "Not found"}, status_code=404)
    body = await req.json()
    s = sessions[sid]
    if "name" in body:
        s.name = body["name"]
    s.save()
    return s.to_dict()


@app.delete("/api/sessions/{sid}")
async def delete_session(sid: str):
    if sid not in sessions:
        return JSONResponse({"error": "Not found"}, status_code=404)
    await sessions[sid].stop()
    fpath = os.path.join(SESSIONS_DIR, f"{sid}.json")
    try:
        os.remove(fpath)
    except OSError:
        pass
    del sessions[sid]
    return {"ok": True}


@app.get("/api/sessions/{sid}/export")
def export_session(sid: str):
    if sid not in sessions:
        return JSONResponse({"error": "Not found"}, status_code=404)
    s = sessions[sid]
    title = s.name or f"Session {s.id}"
    lines = [f"# {title}\n\n"]
    lines.append(f"**작업 디렉토리:** {s.workdir}  \n")
    lines.append(f"**생성일:** {s.created_at}\n\n---\n")
    for item in s.history:
        if item["role"] == "user":
            lines.append(f"\n**👤 사용자**\n\n{item['content']}\n\n---\n")
        elif item["role"] == "gemini":
            lines.append(f"\n**✦ Gemini**\n\n{item['content']}\n\n---\n")
    filename = f"session-{s.id}.md"
    return PlainTextResponse(
        "".join(lines),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        media_type="text/markdown; charset=utf-8",
    )


@app.get("/api/favorites")
def get_favorites():
    try:
        with open(FAVORITES_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []


@app.put("/api/favorites")
async def save_favorites(req: Request):
    body = await req.json()
    with open(FAVORITES_FILE, "w", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=False, indent=2)
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
