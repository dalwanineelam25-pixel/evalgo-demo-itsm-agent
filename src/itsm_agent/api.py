"""HTTP API (FastAPI).

Authentication is a placeholder: the caller's identity comes from the ``X-User-Id`` header, as it
would after an upstream SSO proxy. Do not expose this service beyond localhost as-is.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .config import Settings
from .policy import can_read
from .service import ConversationError, ITSMAgentService

_STATUS = {
    "not_found": 404,
    "unknown_user": 401,
    "forbidden": 403,
    "bad_request": 400,
    "confirmation_pending": 409,
    "no_pending_confirmation": 409,
}


class StartConversation(BaseModel):
    incident_id: str | None = None


class UserMessage(BaseModel):
    message: str = Field(min_length=1, max_length=8000)


class ConfirmationAnswer(BaseModel):
    confirmed: bool
    comment: str | None = Field(default=None, max_length=1000)


def create_app(settings: Settings | None = None, service: ITSMAgentService | None = None) -> FastAPI:
    holder: dict[str, ITSMAgentService] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        holder["svc"] = service or ITSMAgentService(settings or Settings.from_env())
        yield
        if service is None:
            holder["svc"].close()

    app = FastAPI(title="ITSM Incident Agent", version="0.1.0", lifespan=lifespan)

    def svc() -> ITSMAgentService:
        return holder["svc"]

    def user_id(x_user_id: str = Header(..., alias="X-User-Id")) -> str:
        return x_user_id

    def fail(exc: ConversationError) -> HTTPException:
        return HTTPException(status_code=_STATUS.get(exc.code, 400), detail={"code": exc.code, "message": exc.message})

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "model_mode": svc().settings.model_mode}

    # Handlers are sync on purpose: FastAPI runs them in a worker thread, and the graph is sync.
    @app.post("/conversations", status_code=201)
    def start(body: StartConversation, uid: str = Depends(user_id)) -> dict:
        try:
            return {"thread_id": svc().start_conversation(uid, body.incident_id)}
        except ConversationError as exc:
            raise fail(exc) from exc

    @app.post("/conversations/{thread_id}/messages")
    def message(thread_id: str, body: UserMessage, uid: str = Depends(user_id)) -> dict:
        try:
            return svc().send_message(thread_id, uid, body.message).as_dict()
        except ConversationError as exc:
            raise fail(exc) from exc

    @app.post("/conversations/{thread_id}/confirmation")
    def confirmation(thread_id: str, body: ConfirmationAnswer, uid: str = Depends(user_id)) -> dict:
        try:
            return svc().respond_to_confirmation(thread_id, uid, body.confirmed, body.comment).as_dict()
        except ConversationError as exc:
            raise fail(exc) from exc

    @app.get("/conversations/{thread_id}")
    def transcript(thread_id: str, uid: str = Depends(user_id)) -> dict:
        try:
            s = svc()
            return {
                "thread_id": thread_id,
                "messages": s.transcript(thread_id, uid),
                "pending_confirmation": s.pending_confirmation(thread_id),
            }
        except ConversationError as exc:
            raise fail(exc) from exc

    @app.get("/incidents/{number}")
    def incident(number: str, uid: str = Depends(user_id)) -> dict:
        s = svc()
        inc = s.repo.get_incident(number)
        if inc is None:
            raise HTTPException(404, detail={"code": "not_found", "message": f"{number} not found"})
        decision = can_read(s.repo.get_user(uid), inc)
        if not decision.allowed:
            raise HTTPException(403, detail={"code": decision.code, "message": decision.reason})
        return s.repo.incident_snapshot(number)

    return app
