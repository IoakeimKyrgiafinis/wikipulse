"""Helpers for long-running stream clients: backoff, reconnect loop, graceful stop."""

import asyncio
import logging
import random
import signal
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)


class Backoff:
    """Exponential backoff with jitter: about base, 2*base, 4*base, ... capped at max_s.

    Each delay is drawn between half and all of the current step ("equal jitter"), so
    many clients that lost the server at the same moment don't all return together,
    while every wait is still long enough to give the server a break.
    """

    def __init__(
        self,
        base_s: float = 1.0,
        max_s: float = 60.0,
        rand: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.base_s = base_s
        self.max_s = max_s
        self._rand = rand
        self.attempt = 0

    def next_delay(self) -> float:
        """Return the next delay in seconds and step up the backoff."""
        step = min(self.max_s, self.base_s * 2**self.attempt)
        self.attempt += 1
        return step / 2 + self._rand(0, step / 2)

    def reset(self) -> None:
        """Call after a success, so the next failure starts with a short delay again."""
        self.attempt = 0


_END = object()


class _Failure:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc


async def until_stop[T](items: AsyncIterator[T], stop: asyncio.Event) -> AsyncIterator[T]:
    """Yield from `items` until it ends or `stop` is set, whichever comes first.

    A reader task pulls items into a one-slot queue; this generator waits for either
    the next item or the stop request. A stop therefore interrupts *waiting* for the
    network, but never the caller's work on an item it already received.
    Exceptions raised by `items` are re-raised here.
    """
    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=1)

    async def pump() -> None:
        try:
            async for item in items:
                await queue.put(item)
        except Exception as exc:  # handed to the consumer side and re-raised there
            await queue.put(_Failure(exc))
            return
        finally:
            # Close the source in this task, so e.g. its HTTP connection is released here.
            aclose = getattr(items, "aclose", None)
            if aclose is not None:
                await aclose()
        await queue.put(_END)

    pump_task = asyncio.create_task(pump())
    stop_task = asyncio.create_task(stop.wait())
    tasks = [pump_task, stop_task]
    try:
        while True:
            get_task = asyncio.create_task(queue.get())
            tasks[2:] = [get_task]
            await asyncio.wait({get_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
            if stop.is_set():  # a stop request wins, even if another item is ready
                return
            item = get_task.result()
            if item is _END:
                return
            if isinstance(item, _Failure):
                raise item.exc
            yield item
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("Error while closing the source", exc_info=True)


async def run_with_reconnect(
    attempt: Callable[[], Awaitable[None]],
    stop: asyncio.Event,
    backoff: Backoff,
    on_reconnect: Callable[[], Awaitable[None]] | None = None,
    name: str = "stream",
) -> None:
    """Run `attempt` (one connection) again and again until `stop` is set.

    Whenever an attempt ends, because the server closed the stream or because of an
    error, wait with backoff, call `on_reconnect` (e.g. to count it) and try again.
    `attempt` should call `backoff.reset()` once it makes progress.
    """
    while not stop.is_set():
        try:
            await attempt()
            if stop.is_set():
                return
            logger.warning("%s ended unexpectedly", name)
        except Exception as exc:
            logger.warning("%s failed: %s: %s", name, type(exc).__name__, exc)
            logger.debug("Traceback", exc_info=True)
        delay = backoff.next_delay()
        logger.warning("Reconnecting %s in %.1f s (attempt %d)", name, delay, backoff.attempt)
        if await wait_or_stop(stop, delay):
            return
        if on_reconnect is not None:
            try:
                await on_reconnect()
            except Exception:
                logger.warning("Could not record the reconnect", exc_info=True)


async def wait_or_stop(stop: asyncio.Event, seconds: float) -> bool:
    """Sleep up to `seconds`; return True early (and immediately) if `stop` gets set."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        return False
    return True


def install_stop_handlers(stop: asyncio.Event) -> None:
    """Make Ctrl+C (and SIGTERM / Ctrl+Break) set `stop` instead of killing the program.

    A second Ctrl+C raises KeyboardInterrupt, as an escape hatch if shutdown hangs.
    Works on Windows and Linux; must be called from the main thread inside the loop.
    """
    loop = asyncio.get_running_loop()

    def handler(signum: int, frame: Any) -> None:
        if stop.is_set():
            raise KeyboardInterrupt
        logger.info(
            "Received %s, finishing the current message and stopping", signal.Signals(signum).name
        )
        loop.call_soon_threadsafe(stop.set)

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):  # SIGBREAK exists only on Windows
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), handler)
