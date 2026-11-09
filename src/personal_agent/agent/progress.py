"""Request-local activity and bounded, transport-independent chat notifications."""

import asyncio
import logging
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from time import monotonic

from personal_agent.agent.models import ActivityCallback, ActivityEvent

logger = logging.getLogger(__name__)
activity_callback: ContextVar[ActivityCallback | None] = ContextVar("chat_activity", default=None)


def report_activity(event: ActivityEvent) -> None:
    callback = activity_callback.get()
    if callback is not None:
        callback(event)


@dataclass(frozen=True)
class ChatEvent:
    kind: str
    run_id: str
    conversation_id: str
    sequence: int
    text: str | None = None
    code: str | None = None
    timeout_seconds: float | None = None

    def payload(self) -> dict:
        return {k: v for k, v in asdict(self).items() if k != "kind" and v is not None}


ChatEventCallback = Callable[[ChatEvent], None]


class ProgressReporter:
    def __init__(
        self,
        *,
        run_id: str,
        conversation_id: str,
        on_event: ChatEventCallback | None,
        enabled: bool,
        ack_delay: float,
        interval: float,
        max_updates: int,
        timeout_seconds: float,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.run_id = run_id
        self.conversation_id = conversation_id
        self.on_event = on_event
        self.enabled = enabled and on_event is not None
        self.interval = interval
        self.max_updates = max_updates
        self.timeout_seconds = timeout_seconds
        self.clock = clock
        self.next_due = clock() + ack_delay
        self.phase = "waiting"
        self.last_phase: str | None = None
        self.acknowledged = False
        self.updates = 0
        self.sequence = 0
        self.closed = False
        self.timer: asyncio.Task | None = None

    def emit(self, kind: str, **kwargs) -> None:
        self.sequence += 1
        event = ChatEvent(kind, self.run_id, self.conversation_id, self.sequence, **kwargs)
        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception as exc:
                # Observability must not fail a chat or expose callback/request content.
                logger.warning("chat_notification_failed error_type=%s", type(exc).__name__)

    def start(self) -> None:
        self.emit("started", timeout_seconds=self.timeout_seconds)
        if self.enabled:
            self.timer = asyncio.create_task(self._run_timer())

    def activity(self, event: ActivityEvent) -> None:
        if self.closed:
            return
        if event.phase == "tool_started":
            self.phase = "saved_data" if (event.tool_id or "").startswith("portfolio.") else "tool"
        elif event.phase in {"search_completed", "tool_completed"}:
            self.phase = "model_working"
        elif event.phase in {"waiting", "model_working", "searching", "response_started"}:
            self.phase = event.phase

    def emit_due(self) -> None:
        """One notification per elapsed interval; never catch up with a message burst."""
        if self.closed or not self.enabled or self.clock() < self.next_due:
            return
        if self.acknowledged and self.updates >= self.max_updates:
            return
        messages = {
            "waiting": "Your request is waiting to start.",
            "model_working": "I’m working on your request.",
            "searching": "I’m searching public sources.",
            "tool": "I’m checking the available information.",
            "saved_data": "I’m checking the saved portfolio information.",
            "response_started": "I’m preparing the response.",
        }
        text = messages[self.phase]
        if self.acknowledged and self.phase == self.last_phase:
            text = "This is taking a little longer; I’m still working on it."
        kind = "status" if self.acknowledged else "acknowledgment"
        if self.acknowledged:
            self.updates += 1
        self.acknowledged = True
        self.last_phase = self.phase
        self.next_due = self.clock() + self.interval
        self.emit(kind, text=text)

    async def _run_timer(self) -> None:
        while not self.closed:
            await asyncio.sleep(max(0, self.next_due - self.clock()))
            # Give a completing request priority over a notification due this tick.
            await asyncio.sleep(0)
            self.emit_due()
            if self.acknowledged and self.updates >= self.max_updates:
                return

    def finish(self, kind: str, *, text: str, code: str | None = None) -> None:
        if self.closed:
            return
        self.closed = True
        self.emit(kind, text=text, code=code)

    async def aclose(self) -> None:
        self.closed = True
        if self.timer is not None:
            self.timer.cancel()
            try:
                await self.timer
            except asyncio.CancelledError:
                pass


class ChatEventBuffer:
    """Keep started, latest pending progress, and terminal without blocking execution."""

    def __init__(self) -> None:
        self.started: ChatEvent | None = None
        self.progress: ChatEvent | None = None
        self.terminal: ChatEvent | None = None
        self.ready = asyncio.Event()

    def put(self, event: ChatEvent) -> None:
        if self.terminal is not None:
            return
        if event.kind == "started":
            self.started = event
        elif event.kind in {"result", "error"}:
            self.terminal = event
            self.progress = None
        else:
            self.progress = event
        self.ready.set()

    async def get(self) -> ChatEvent:
        while True:
            if self.started is not None:
                event, self.started = self.started, None
                return event
            if self.terminal is not None:
                return self.terminal
            if self.progress is not None:
                event, self.progress = self.progress, None
                return event
            self.ready.clear()
            await self.ready.wait()
