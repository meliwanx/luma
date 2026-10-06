"""Authenticated ideas, feedback, and side-chat launch endpoints."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, ConfigDict

from ..auth import current_user_id
from ..models import SessionCreate
from ..services import ideas
from .sessions import create_session


router = APIRouter()


class IdeaFeedback(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["more_like", "not_interested"]


def _user_id(request: Request) -> str:
    user_id = current_user_id(request, required=True)
    request.state.luma_user_id = user_id
    return user_id


@router.get("/api/v1/ideas")
def list_ideas(request: Request) -> dict:
    return ideas.list_ideas(_user_id(request))


@router.post("/api/v1/ideas/refresh", status_code=status.HTTP_202_ACCEPTED)
def refresh_ideas(request: Request) -> dict:
    generating = ideas.trigger_generation(_user_id(request), manual=True)
    return {"generating": generating}


@router.post("/api/v1/ideas/{idea_id}/feedback", status_code=status.HTTP_204_NO_CONTENT)
def feedback_idea(request: Request, idea_id: str, payload: IdeaFeedback) -> Response:
    ideas.record_feedback(_user_id(request), idea_id, payload.action)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/api/v1/ideas/{idea_id}/start")
def start_idea(request: Request, idea_id: str) -> dict:
    user_id = _user_id(request)
    item = ideas.get_owned_idea(user_id, idea_id)
    # Return the DB connection before invoking the normal session constructor;
    # starting an idea must work with the shared two-connection pool.
    session = create_session(request, SessionCreate(title=item["title"]))
    ideas.mark_started(user_id, idea_id)
    return {"session_id": session.id, "prompt": "我们开始做这个：" + item["title"] + "\n\n" + item["plan_markdown"]}
