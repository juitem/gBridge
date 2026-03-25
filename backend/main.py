import asyncio
import os
import subprocess
import json
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
import uvicorn

app = FastAPI()


@app.get("/api/browse")
def browse(path: str = "/"):
    """List directories at given path."""
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


@app.websocket("/ws/gemini")
async def gemini_ws(websocket: WebSocket):
    await websocket.accept()
    try:
        # First message: config { workdir, resume, prompt }
        config_raw = await websocket.receive_text()
        config = json.loads(config_raw)
        workdir = config.get("workdir", os.path.expanduser("~"))
        resume = config.get("resume", False)
        prompt = config.get("prompt", "")

        gemini_path = subprocess.run(
            ["which", "gemini"], capture_output=True, text=True
        ).stdout.strip() or "gemini"

        cmd = [gemini_path, "-p", prompt]
        if resume:
            cmd += ["--resume", "latest"]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        # Stream output line by line
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            await websocket.send_text(line.decode("utf-8", errors="replace"))

        await proc.wait()
        await websocket.send_text("\x00DONE")

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_text(f"\n[오류] {e}\n")
        except Exception:
            pass


# Serve frontend
app.mount("/", StaticFiles(directory="frontend", html=True), name="static")

if __name__ == "__main__":
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8765, reload=True)
