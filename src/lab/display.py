"""Own one terminal application across successive interactive pages."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from prompt_toolkit.application.current import get_app_session

from lab.errors import LabError

if TYPE_CHECKING:
    from lab.ui import UI

_current: ContextVar[Display | None] = ContextVar("lab_display", default=None)


class Display:
    def __init__(self):
        self.output = get_app_session().output
        self.view: UI | None = None
        self.task: asyncio.Task | None = None
        self.owner: asyncio.Task | None = None

    def page(self, context, badge) -> UI:
        from lab.ui import UI

        if self.view is None:
            self.view = UI(badge=badge, output=self.output, exit_message=None)
            self.view.on_exit = self.cancel
        self.owner = asyncio.current_task()
        self.view.context, self.view.badge = context, badge
        self.view.reply = asyncio.get_running_loop().create_future()
        return self.view

    def cancel(self):
        if self.view and self.view.reply and not self.view.reply.done():
            self.view.reply.set_result(None)
        elif self.owner:
            self.owner.cancel()

    async def show(self, view: UI) -> Any:
        reply = view.reply
        assert view is self.view and reply is not None
        if self.task is None:
            self.task = asyncio.create_task(view.run())
        try:
            done, _ = await asyncio.wait((reply, self.task), return_when=asyncio.FIRST_COMPLETED)
            if self.task in done:
                self.task.result()
                raise LabError("Terminal interface closed.", 130)
            return reply.result()
        finally:
            view.accepting = False
            if not reply.done():
                reply.cancel()
            if view.reply is reply:
                view.reply = None

    async def close(self):
        view, task = self.view, self.task
        if task is not None:
            if not task.done():
                if view is not None and view.app is not None and view.app.is_running:
                    view.app.exit()
                else:
                    task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if view is not None:
            view.clear()
            view.app = None
        self.view, self.task, self.owner = None, None, None


def current_display() -> Display | None:
    return _current.get()


@asynccontextmanager
async def display_session(enabled=True):
    """Keep pages on one surface until session exit or explicit terminal handoff."""
    if not enabled or _current.get() is not None:
        yield
        return
    display = Display()
    token = _current.set(display)
    try:
        yield
    finally:
        try:
            await display.close()
        finally:
            _current.reset(token)


async def release_display():
    """Release input and erase the owned region before a shell or sensitive clear."""
    display = _current.get()
    if display is not None:
        await display.close()
