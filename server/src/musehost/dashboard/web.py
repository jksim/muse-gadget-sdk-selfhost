"""The dashboard's web side: guard, sessions, login, the CA page, the layout.

Mounted at ``/dashboard`` on the gadget-facing app (port 443, the host CA's
certificate). With no password set, everything but ``/ca`` is a 404. Otherwise
a session is needed, except for ``/login``, ``/ca`` and ``/static``; POSTs
also need the session's CSRF token (``X-CSRF-Token``, as htmx sends it, or a
``csrf`` form field). Every response carries a strict Content-Security-Policy.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from musehost.dashboard import auth, logring

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))

COOKIE = "musehost_dash"
PREFIX = "/dashboard"
IDLE_S = 30 * 24 * 3600
SEEN_EVERY_S = 60  # don't write last_seen on every request
FAILURES_ALLOWED = 5
FAILURE_WINDOW_S = 60
BLOCK_S = 15 * 60
PUBLIC = ("/ca", "/ca.pem", "/login", "/static/")
HEADERS = {
    "content-security-policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
    "cache-control": "no-store",
}


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Sessions:
    def __init__(self, store, clock=time.time) -> None:
        self.store = store
        self.clock = clock

    def create(self) -> tuple[str, str]:
        token, csrf, now = secrets.token_urlsafe(32), secrets.token_urlsafe(32), self.clock()
        self.store.db.execute(
            "INSERT INTO dashboard_sessions (token_hash, csrf, created_at, last_seen)"
            " VALUES (?, ?, ?, ?)",
            (_token_hash(token), csrf, now, now),
        )
        return token, csrf

    def lookup(self, token: str | None) -> dict | None:
        """The live session for ``token`` (with its CSRF token), or None."""
        if not token:
            return None
        key = _token_hash(token)
        row = self.store.db.execute(
            "SELECT csrf, last_seen FROM dashboard_sessions WHERE token_hash = ?", (key,)
        ).fetchone()
        now = self.clock()
        if row is None:
            return None
        if now - row["last_seen"] > IDLE_S:
            self.store.db.execute("DELETE FROM dashboard_sessions WHERE token_hash = ?", (key,))
            return None
        if now - row["last_seen"] > SEEN_EVERY_S:
            self.store.db.execute(
                "UPDATE dashboard_sessions SET last_seen = ? WHERE token_hash = ?", (now, key)
            )
        return {"token_hash": key, "csrf": row["csrf"]}

    def end(self, token_hash: str) -> None:
        self.store.db.execute("DELETE FROM dashboard_sessions WHERE token_hash = ?", (token_hash,))


class RateLimit:
    """Wrong passwords per client address: 5 a minute, then 15 minutes out."""

    def __init__(self, clock=time.monotonic) -> None:
        self.clock = clock
        self.failures: dict[str, deque] = defaultdict(deque)
        self.blocked_until: dict[str, float] = {}

    def blocked(self, who: str) -> bool:
        return self.blocked_until.get(who, 0) > self.clock()

    def failed(self, who: str) -> None:
        now = self.clock()
        recent = self.failures[who]
        recent.append(now)
        while recent and now - recent[0] > FAILURE_WINDOW_S:
            recent.popleft()
        if len(recent) >= FAILURES_ALLOWED:
            self.blocked_until[who] = now + BLOCK_S
            recent.clear()


def _with_headers(app):
    async def wrapped(scope, receive, send):
        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                present = {k.lower() for k, _ in message.get("headers", [])}
                extra = [
                    (k.encode(), v.encode())
                    for k, v in HEADERS.items()
                    if k.encode() not in present
                ]
                message = {**message, "headers": [*message.get("headers", []), *extra]}
            await send(message)

        await app(scope, receive, send_with_headers)

    return wrapped


def _guard(app, state: Path, sessions_for):
    """Off → 404 (except /ca); signed out → login; POST → CSRF."""

    async def wrapped(scope, receive, send):
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        path = scope.get("path", "")
        sub = path[len(PREFIX) :] if path.startswith(PREFIX) else path
        sub = sub or "/"
        if sub in ("/ca", "/ca.pem"):
            await app(scope, receive, send)
            return
        if not auth.is_enabled(state):
            await PlainTextResponse("Not Found", status_code=404)(scope, receive, send)
            return
        if sub == "/login" or sub.startswith("/static/"):
            await app(scope, receive, send)
            return
        request = Request(scope, receive)
        session = sessions_for().lookup(request.cookies.get(COOKIE))
        if session is None:
            if scope["method"] == "GET":
                response = RedirectResponse(f"{PREFIX}/login", status_code=303)
            else:
                response = PlainTextResponse("Sign in first", status_code=401)
            await response(scope, receive, send)
            return
        scope.setdefault("state", {})["dashboard_session"] = session
        await app(scope, receive, send)

    return wrapped


async def require_csrf(request: Request) -> bool:
    """True when the request carries this session's CSRF token."""
    session = request.scope.get("state", {}).get("dashboard_session")
    if session is None:
        return False
    given = request.headers.get("x-csrf-token")
    if given is None and request.method == "POST":
        form = await request.form()
        given = form.get("csrf")
    return bool(given) and hmac.compare_digest(str(given), session["csrf"])


def page(request: Request, name: str, context: dict | None = None, status_code: int = 200):
    session = request.scope.get("state", {}).get("dashboard_session")
    return templates.TemplateResponse(
        request,
        name,
        {"csrf": session["csrf"] if session else "", "signed_in": bool(session), **(context or {})},
        status_code=status_code,
    )


KEY_FOR = {
    "claude": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "vllm": "VLLM_API_KEY",
    "hermes": "HERMES_API_KEY",
}


def _uptime(seconds: float) -> str:
    minutes, _ = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h" if days else f"{hours} h {minutes} min"


def status_of(parent, state: Path) -> dict:
    """What the status page shows; never a key, token or conversation."""
    import importlib.metadata
    import os

    from musehost.cli import ca_fingerprint

    app_state = parent.state
    config = app_state.config
    handler = app_state.hub.chat_handler
    provider = getattr(handler, "provider", None)
    brain = None
    if provider is not None:
        key_name = KEY_FOR.get(provider.name)
        brain = {
            "provider": provider.name,
            "model": provider.model,
            "key": "key set"
            if key_name and os.environ.get(key_name)
            else "no key needed"
            if not key_name
            else "key missing",
        }
    mcp = getattr(app_state, "mcp_server", None)
    devices = app_state.tokens.store.db.execute(
        "SELECT COUNT(*) FROM devices WHERE revoked_at IS NULL"
    ).fetchone()[0]
    online = sum(1 for d in app_state.hub.devices() if d.online)
    try:
        version = importlib.metadata.version("musehost")
    except importlib.metadata.PackageNotFoundError:
        version = "?"
    return {
        "version": version,
        "uptime": _uptime(time.time() - getattr(app_state, "started_at", time.time())),
        "host": config.public_host,
        "fingerprint": ca_fingerprint((state / "ca.pem").read_bytes()),
        "speech": getattr(getattr(app_state, "transcriber", None), "state", "off"),
        "speech_model": config.speech_model,
        "voice": getattr(getattr(app_state, "synthesizer", None), "state", "off"),
        "tts_voice": config.tts_voice,
        "brain": brain,
        "mcp_port": mcp.config.port if mcp is not None else None,
        "paired": devices,
        "online": online,
        "lines": logring.install().lines(),
    }


def create_dashboard(state: Path, parent) -> object:
    """The /dashboard ASGI app, using ``parent.state`` (hub, tokens, config)."""
    limiter = RateLimit()

    def sessions() -> Sessions:
        return Sessions(parent.state.tokens.store)

    async def ca_page(request: Request):
        from musehost.cli import ca_fingerprint

        pem = (state / "ca.pem").read_bytes()
        return page(
            request,
            "ca.html",
            {"fingerprint": ca_fingerprint(pem), "host": parent.state.config.public_host},
        )

    async def ca_pem(request: Request):
        return Response(
            (state / "ca.pem").read_bytes(),
            media_type="application/x-pem-file",
            headers={"content-disposition": 'attachment; filename="musehost-ca.pem"'},
        )

    async def login(request: Request):
        who = request.client.host if request.client else "?"
        if request.method == "GET":
            return page(request, "login.html")
        if limiter.blocked(who):
            return page(
                request,
                "login.html",
                {"error": "Too many wrong passwords. Try again in 15 minutes."},
                status_code=429,
            )
        form = await request.form()
        if not auth.check_password(state, str(form.get("password") or "")):
            limiter.failed(who)
            return page(
                request, "login.html", {"error": "That's not the password."}, status_code=401
            )
        token, _ = sessions().create()
        response = RedirectResponse(PREFIX, status_code=303)
        response.set_cookie(
            COOKIE,
            token,
            max_age=IDLE_S,
            path=PREFIX,
            secure=True,
            httponly=True,
            samesite="strict",
        )
        return response

    async def logout(request: Request):
        if not await require_csrf(request):
            return PlainTextResponse("Missing or wrong CSRF token", status_code=403)
        sessions().end(request.scope["state"]["dashboard_session"]["token_hash"])
        response = RedirectResponse(f"{PREFIX}/login", status_code=303)
        response.delete_cookie(COOKIE, path=PREFIX, secure=True, httponly=True, samesite="strict")
        return response

    async def home(request: Request):
        return page(request, "status.html", {"title": "Status", **status_of(parent, state)})

    async def logs(request: Request):
        return page(request, "_logs.html", {"lines": logring.install().lines()})

    from musehost.dashboard import gadgets, settings

    routes = [
        *settings.routes(state, parent),
        *gadgets.routes(state, parent),
        Route("/", home),
        Route("/ca", ca_page),
        Route("/ca.pem", ca_pem),
        Route("/login", login, methods=["GET", "POST"]),
        Route("/logout", logout, methods=["POST"]),
        Route("/logs", logs),
        Mount("/static", app=StaticFiles(directory=str(HERE / "static")), name="static"),
    ]
    inner = Starlette(routes=routes)
    inner.state.parent = parent
    return _with_headers(_guard(inner, state, sessions))
