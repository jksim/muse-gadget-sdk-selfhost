"""Chat with Clio as the operator, as `musehost chat` does in a terminal.

A turn runs as a ``chat`` job (one at a time); its reply streams over SSE as
``piece`` events and ends with ``done``. The text is never logged.
"""

from __future__ import annotations

from pathlib import Path

from markupsafe import escape
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, StreamingResponse
from starlette.routing import Route

from musehost import chat
from musehost.dashboard.jobs import Busy, Jobs
from musehost.dashboard.web import page, require_csrf

MAX_MESSAGE = 4000
FAILED_REPLY = " (Something went wrong on my side.)"


def _piece(stamp: float, text: str) -> tuple[str, str]:
    return "piece", str(escape(text))


def _finished(job) -> tuple[str, str]:
    return "done", "" if job.state == "succeeded" else str(escape(FAILED_REPLY))


def routes(state: Path, parent) -> list[Route]:
    if not hasattr(parent.state, "jobs"):
        parent.state.jobs = Jobs()

    def jobs() -> Jobs:
        return parent.state.jobs

    def gadgets() -> list:
        return [d for d in parent.state.hub.devices() if d.online]

    async def chat_page(request: Request):
        return page(
            request,
            "chat.html",
            {"title": "Chat", "gadgets": gadgets(), "fresh": request.query_params.get("new")},
        )

    async def send(request: Request):
        if not await require_csrf(request):
            return PlainTextResponse("Missing or wrong CSRF token", status_code=403)
        form = await request.form()
        message = str(form.get("message", "")).strip()
        device = str(form.get("device", "")) or None
        if not message or len(message) > MAX_MESSAGE:
            return PlainTextResponse(
                f"Write a message (up to {MAX_MESSAGE} characters).", status_code=400
            )
        if device and device not in {d.node_id for d in gadgets()}:
            return PlainTextResponse("That gadget isn't online.", status_code=400)
        hub = parent.state.hub
        name = next((d.display_name for d in gadgets() if d.node_id == device), None)

        async def work(progress):
            async for piece in chat.operator_turn(hub, message, device=device):
                if piece:
                    progress(piece)

        timeout = float(parent.state.config.brain_timeout_s) + 30
        try:
            job = jobs().start("chat", work, timeout, announce_end=False)
        except Busy:
            return PlainTextResponse(
                "Clio is still answering; wait for her reply.", status_code=409
            )
        return page(request, "_turn.html", {"message": message, "job": job, "with": name or device})

    async def events(request: Request):
        job = jobs().get(request.path_params["job_id"])
        if job is None or job.kind != "chat":
            return PlainTextResponse("No such turn", status_code=404)
        last = request.headers.get("last-event-id")
        return StreamingResponse(
            jobs().events(
                job,
                request.is_disconnected,
                after=int(last) if last and last.isdigit() else None,
                line=_piece,
                done=_finished,
            ),
            media_type="text/event-stream",
            headers={"x-accel-buffering": "no"},
        )

    async def new(request: Request):
        if not await require_csrf(request):
            return PlainTextResponse("Missing or wrong CSRF token", status_code=403)
        history = getattr(parent.state.hub.chat_handler, "history", None)
        if history is not None:
            history.start_new(chat.OPERATOR)
        return RedirectResponse("/dashboard/chat?new=1", status_code=303)

    return [
        Route("/chat", chat_page),
        Route("/chat/send", send, methods=["POST"]),
        Route("/chat/new", new, methods=["POST"]),
        Route("/chat/turns/{job_id}/events", events),
    ]
