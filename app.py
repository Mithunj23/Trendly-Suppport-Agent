"""
app.py
------
Thin FastAPI wrapper: one /chat endpoint for the agent, in-memory session
store keyed by a session_id the client generates, and a static demo UI.

Run: uvicorn app:app --host 0.0.0.0 --port 8000
"""

import os
import uuid
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from agent import Session, run_turn
import tools

app = FastAPI(title="Trendly Support Agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory session store. Fine for a demo / single-instance deployment;
# swap for Redis or a DB for multi-instance production use (see SOLUTION.md).
_SESSIONS: dict[str, Session] = {}


class ChatRequest(BaseModel):
    session_id: str | None = None
    message: str


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    tool_trace: list


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    session_id = req.session_id or str(uuid.uuid4())
    session = _SESSIONS.get(session_id)
    if session is None:
        session = Session()
        _SESSIONS[session_id] = session

    if not req.message or not req.message.strip():
        raise HTTPException(400, "message must not be empty")

    result = run_turn(session, req.message.strip())
    return ChatResponse(session_id=session_id, reply=result["reply"], tool_trace=result["tool_trace"])


@app.post("/reset")
def reset(session_id: str):
    _SESSIONS.pop(session_id, None)
    return {"ok": True}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/debug/actions")
def debug_actions():
    """Everything the agent has actually done (returns/exchanges/escalations) — for demo purposes."""
    return tools.get_actions_log()


app.mount("/", StaticFiles(directory="static", html=True), name="static")
