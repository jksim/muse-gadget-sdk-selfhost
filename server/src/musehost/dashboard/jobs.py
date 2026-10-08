"""Background jobs (pairing, flashing) that outlive the page that started them.

One job of each kind at a time. A job is a coroutine function given a
``progress(text)`` callback; its lines become the job's events, streamed over
SSE with ids, so a browser that reconnects resumes where it was. Jobs keep only
their events and result: whatever the function closed over (a Wi-Fi password)
goes when it ends.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field

from markupsafe import escape

log = logging.getLogger(__name__)
KEEP_FINISHED = 5


class Busy(Exception):
    pass


@dataclass
class Job:
    id: str
    kind: str
    state: str = "running"  # running | succeeded | failed
    events: list[tuple[float, str]] = field(default_factory=list)
    result: dict | None = None
    reason: str = ""
    started: float = field(default_factory=time.time)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    _more: asyncio.Event = field(default_factory=asyncio.Event)
    _task: asyncio.Task | None = None

    def progress(self, text: str) -> None:
        self.events.append((time.time(), str(text)))
        self._more.set()


class Jobs:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def running(self, kind: str) -> Job | None:
        return next(
            (j for j in self._jobs.values() if j.kind == kind and j.state == "running"), None
        )

    def start(self, kind: str, work, timeout_s: float, announce_end: bool = True) -> Job:
        if self.running(kind) is not None:
            raise Busy(kind)
        job = Job(id=secrets.token_urlsafe(9), kind=kind)
        self._jobs[job.id] = job
        finished = [j for j in self._jobs.values() if j.state != "running"]
        for old in finished[:-KEEP_FINISHED]:
            del self._jobs[old.id]
        job._task = asyncio.get_running_loop().create_task(
            self._run(job, work, timeout_s, announce_end)
        )
        return job

    async def _run(self, job: Job, work, timeout_s: float, announce_end: bool) -> None:
        log.info("job %s (%s) started", job.id, job.kind)
        try:
            async with asyncio.timeout(timeout_s):
                job.result = await work(job.progress)
            job.state = "succeeded"
        except TimeoutError:
            job.state, job.reason = "failed", f"it took too long (over {timeout_s:g} s)"
        except Exception as exc:
            job.state, job.reason = "failed", str(exc) or type(exc).__name__
        finally:
            if job.state == "running":  # cancelled
                job.state, job.reason = "failed", "stopped"
            if announce_end:
                job.progress("Done." if job.state == "succeeded" else f"Failed: {job.reason}")
            log.info("job %s (%s) %s", job.id, job.kind, job.state)
            job.done.set()

    async def events(
        self,
        job: Job,
        is_disconnected,
        after: int | None = None,
        keepalive_s: float = 15,
        line=None,
        done=None,
    ):
        """SSE: each event after id ``after``, following until the job ends.

        ``line(stamp, text)`` and ``done(job)`` give an event's name and HTML;
        by default timestamped ``line`` steps and a ``done`` state.
        """
        line = line or _step
        done = done or _state
        index = 0 if after is None else after + 1
        while True:
            job._more.clear()
            while index < len(job.events):
                name, html = line(*job.events[index])
                yield _event(name, html, index)
                index += 1
            if job.done.is_set():
                yield _event(*done(job))
                return
            if await is_disconnected():
                return
            try:
                await asyncio.wait_for(job._more.wait(), keepalive_s)
            except TimeoutError:
                yield ": keepalive\n\n"


def _event(name: str, html: str, index: int | None = None) -> str:
    head = f"id: {index}\n" if index is not None else ""
    data = "".join(f"data: {part}\n" for part in html.split("\n"))
    return f"{head}event: {name}\n{data}\n"


def _step(stamp: float, text: str) -> tuple[str, str]:
    clock = time.strftime("%H:%M:%S", time.localtime(stamp))
    return "line", f'<li><span class="muted">{clock}</span> {escape(text)}</li>'


def _state(job: Job) -> tuple[str, str]:
    style = "ok" if job.state == "succeeded" else "error"
    return "done", f'<span class="{style}">{job.state}</span>'
