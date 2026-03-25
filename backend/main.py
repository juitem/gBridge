import asyncio
import os
import subprocess
import json
import uuid
from datetime import datetime
from typing import Dict, List
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
import uvicorn

app = FastAPI()

# ── Session Store ────────────────────────────────────────────────────────────

class Session:
    def __init__(self, session_id: str, workdir: str, resume: bool):
        self.id = session_id
        self.workdir = workdir
        self.resume = resume
        self.created_at = datetime.now().isoformat()
        self.clients: List[WebSocket] = []
        self.history: List[dict] = []  # [{role, content}]

    def to_dict(self):
        return {
            "id": self.id,
            "workdir": self.workdir,
            "resume": self.resume,
            "created_at": self.created_at,
            "message_count": len(self.history),
        }

sessions: Dict[str, Session] = {}


# ── API ──────────────────────────────────────────────────────────────────────

@app.get("/api/sessions")
def list_sessions():
    return [s.to_dict() for s in sessions.values()]


@app.post("/api/sessions")
async def create_session(body: dict):
    sid = str(uuid.uuid4())[:8]
    workdir = body.get("workdir", os.path.expanduser("~"))
    resume = body.get("resume", False)
    sessions[sid] = Session(sid, workdir, resume)
    return {"id": sid}


@app.delete("/api/sessions/{sid}")
def delete_session(sid: str):
    if sid in sessions:
        del sessions[sid]
        return {"ok": True}
    return JSONResponse({"error": "Not found"}, status_code=404)


@app.get("/api/browse")
def browse(path: str = "/"):
    try:
        path = os.path.expanduser(path)
        if not os.path.isdir(path):
            return JSONResponse({"error": "Not a directory"}, status_code=400)
        entries = []
        for name in sorted(os.listdir(path)):
            full = os.path.join(path, name)
            if os.path.isdir(full) and not name.startswith("."):
                entries.append({"name": name, "path": full})
        parent = str(os.path.dirname(path)) if path != "/" else None
        return {"current": path, "parent": parent, "entries": entries}
    except PermissionError:
        return JSONResponse({"error": "Permission denied"}, status_code=403)


# ── WebSocket ────────────────────────────────────────────────────────────────

async def broadcast(session: Session, msg: dict):
    """Send message to all connected clients and store in history."""
    text = json.dumps(msg, ensure_ascii=False)
    dead = []
    for ws in session.clients:
        try:
            await ws.send_text(text)
        except Exception:
            dead.append(ws)
    for ws in dead:
        session.clients.remove(ws)


@app.websocket("/ws/session/{sid}")
async def session_ws(websocket: WebSocket, sid: str):
    if sid not in sessions:
        await websocket.close(code=4004)
        return

    session = sessions[sid]
    await websocket.accept()
    session.clients.append(websocket)

    # Replay history to new client
    for msg in session.history:
        try:
            await websocket.send_text(json.dumps(msg, ensure_ascii=False))
        except Exception:
            break

    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            prompt = data.get("prompt", "").strip()
            if not prompt:
                continue

            # Record user message
            user_msg = {"role": "user", "content": prompt}
            session.history.append(user_msg)
            await broadcast(session, user_msg)

            # Run Gemini CLI
            gemini_path = subprocess.run(
                ["which", "gemini"], capture_output=True, text=True
            ).stdout.strip() or "gemini"

            cmd = [gemini_path, "-p", prompt]
            if session.resume or len(session.history) > 2:
                cmd += ["--resume", "latest"]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=session.workdir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

            # Stream response chunks to all clients
            gemini_content = ""
            await broadcast(session, {"role": "gemini_start"})

            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                chunk = line.decode("utf-8", errors="replace")
                gemini_content += chunk
                await broadcast(session, {"role": "gemini_chunk", "content": chunk})

            await proc.wait()

            # Store full response in history
            session.history.append({"role": "gemini", "content": gemini_content})
            await broadcast(session, {"role": "gemini_done"})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await broadcast(session, {"role": "error", "content": str(e)})
        except Exception:
            pass
    finally:
        if websocket in session.clients:
            session.clients.remove(websocket)


# Serve frontend
app.mount("/", StaticFiles(directory="frontend", html=True), name="static")

if __name__ == "__main__":
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8765, reload=True)
