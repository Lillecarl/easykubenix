"""Where the profiler starts decides whether it can attribute a wait.

pyinstrument records the async context it started in. Started around
`asyncio.run`, every coroutine is out of context and the whole wait lands in
the event loop's selector -- which is the one thing a profile of `ekn
kubeapply` must not do, since a deploy is almost entirely waiting.

Found by `solid-kubernetes` against a live cluster: `ekn clusterdiff` put
11.803s of a 16.439s run into a single `selectors.py:select`, 72%, against
cProfile's 73% in `epoll.poll`. Same shape, so the new backend bought
nothing until this moved.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

pyinstrument = pytest.importorskip("pyinstrument")

SLEEP = 0.05
COUNT = 6


async def waits_here() -> None:
    await asyncio.sleep(SLEEP)


async def sequentially() -> None:
    for _ in range(COUNT):
        await waits_here()


async def fanned_out() -> None:
    await asyncio.gather(*(waits_here() for _ in range(COUNT)))


def _frames(frame: dict[str, Any]):
    yield frame
    for child in frame.get("children") or []:
        yield from _frames(child)


def _split(profiler: pyinstrument.Profiler) -> tuple[float, float]:
    """Seconds charged to the loop's selector, and to the awaiting function."""
    from pyinstrument.renderers import JSONRenderer

    root = json.loads(profiler.output(renderer=JSONRenderer()))["root_frame"] or {}
    selector = 0.0
    awaiting = 0.0
    for frame in _frames(root):
        where = f"{frame.get('file_path_short') or ''}:{frame.get('function') or ''}"
        if "selectors" in where or "_run_once" in where:
            selector = max(selector, frame["time"])
        if frame.get("function") == "waits_here":
            awaiting = max(awaiting, frame["time"])
    return selector, awaiting


@pytest.mark.parametrize("work", [sequentially, fanned_out], ids=["sequential", "fanned-out"])
def test_starting_inside_the_loop_attributes_the_wait(work) -> None:
    async def run() -> pyinstrument.Profiler:
        profiler = pyinstrument.Profiler(interval=0.001, async_mode="enabled")
        profiler.start()
        try:
            await work()
        finally:
            profiler.stop()
        return profiler

    selector, awaiting = _split(asyncio.run(run()))

    assert awaiting > 0, "the awaiting frame holds no time"
    assert selector == 0, f"{selector}s landed in the loop's selector"


@pytest.mark.parametrize("work", [sequentially, fanned_out], ids=["sequential", "fanned-out"])
def test_starting_outside_the_loop_loses_the_wait(work) -> None:
    """The arm that documents why the other one is written that way.

    Without this the first test passes for any placement that happens to
    work, and nothing records what the wrong one does.
    """
    profiler = pyinstrument.Profiler(interval=0.001, async_mode="enabled")
    profiler.start()
    asyncio.run(work())
    profiler.stop()

    selector, awaiting = _split(profiler)

    assert selector > 0, "expected the selector to swallow the wait"
    assert awaiting == 0, "expected the awaiting frame to hold nothing"
