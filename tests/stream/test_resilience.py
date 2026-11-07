"""Backoff, the stoppable iterator, and Ctrl+C handling."""

import asyncio
import signal
from collections.abc import AsyncIterator

import pytest

from wikipulse.stream.resilience import Backoff, install_stop_handlers, until_stop, wait_or_stop


def test_backoff_grows_exponentially_and_is_capped() -> None:
    backoff = Backoff(base_s=1, max_s=10, rand=lambda low, high: high)  # always the top
    assert [backoff.next_delay() for _ in range(6)] == [1, 2, 4, 8, 10, 10]


def test_backoff_jitter_stays_between_half_and_full_step() -> None:
    backoff = Backoff(base_s=1, max_s=60)
    for attempt in range(10):
        step = min(60, 2**attempt)
        assert step / 2 <= backoff.next_delay() <= step


def test_backoff_reset() -> None:
    backoff = Backoff(base_s=1, max_s=60, rand=lambda low, high: low)
    backoff.next_delay()
    backoff.next_delay()
    backoff.reset()
    assert backoff.next_delay() == 0.5


async def numbers(n: int, then_raise: bool = False, then_block: bool = False) -> AsyncIterator[int]:
    for i in range(n):
        yield i
    if then_raise:
        raise ConnectionError("boom")
    if then_block:
        await asyncio.Event().wait()


async def test_until_stop_passes_everything_through() -> None:
    assert [i async for i in until_stop(numbers(5), asyncio.Event())] == [0, 1, 2, 3, 4]


async def test_until_stop_reraises_source_errors() -> None:
    seen = []
    with pytest.raises(ConnectionError):
        async for i in until_stop(numbers(3, then_raise=True), asyncio.Event()):
            seen.append(i)
    assert seen == [0, 1, 2]


async def test_until_stop_interrupts_a_waiting_source() -> None:
    stop = asyncio.Event()
    seen = []
    async for i in until_stop(numbers(3, then_block=True), stop):
        seen.append(i)
        if i == 2:
            asyncio.get_running_loop().call_later(0.05, stop.set)
    assert seen == [0, 1, 2]  # returned although the source never ends


async def test_until_stop_closes_the_source() -> None:
    closed = asyncio.Event()

    async def source() -> AsyncIterator[int]:
        try:
            yield 1
            await asyncio.Event().wait()
        finally:
            closed.set()

    stop = asyncio.Event()
    async for _ in until_stop(source(), stop):
        stop.set()
    assert closed.is_set()


async def test_wait_or_stop() -> None:
    stop = asyncio.Event()
    assert await wait_or_stop(stop, 0.01) is False
    stop.set()
    assert await wait_or_stop(stop, 10) is True


async def test_ctrl_c_sets_the_stop_event() -> None:
    previous = signal.getsignal(signal.SIGINT)
    stop = asyncio.Event()
    try:
        install_stop_handlers(stop)
        signal.raise_signal(signal.SIGINT)
        await asyncio.wait_for(stop.wait(), timeout=1)
        with pytest.raises(KeyboardInterrupt):  # a second Ctrl+C forces the exit
            signal.raise_signal(signal.SIGINT)
    finally:
        signal.signal(signal.SIGINT, previous)
        for name in ("SIGTERM", "SIGBREAK"):
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), signal.SIG_DFL)
    assert stop.is_set()
