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
    """Seconds charged to the loop's selector, and to the awaiting function.

    **`selectors` only, never `_run_once`.** `base_events._run_once` is the
    loop's per-iteration driver and an *ancestor* of the awaited work, so its
    time includes the wait even when that wait is attributed correctly. It
    reports the full wait in both arms and separates nothing. Measured over
    49 runs: every spurious failure charged `_run_once` at approximately the
    whole wait, while `selectors.py:select` held 0.001-0.004s. The negative
    arm loses nothing -- `selectors.py:select` appeared there in 24 of 24
    runs, holding the full wait.
    """
    from pyinstrument.renderers import JSONRenderer

    root = json.loads(profiler.output(renderer=JSONRenderer()))["root_frame"] or {}
    selector = 0.0
    awaiting = 0.0
    for frame in _frames(root):
        where = f"{frame.get('file_path_short') or ''}:{frame.get('function') or ''}"
        if "selectors" in where:
            selector = max(selector, frame["time"])
        if frame.get("function") == "waits_here":
            awaiting = max(awaiting, frame["time"])
    return selector, awaiting


class _FakeProfiler:
    """Feeds `_split` a frame tree directly. The two timing tests below are
    sampled, so they cannot fail reliably on a `_split` that charges the
    wrong frame -- this one can."""

    def __init__(self, root: dict[str, Any]) -> None:
        self._root = root

    def output(self, renderer: object) -> str:
        return json.dumps({"root_frame": self._root})


def _frame(path: str, function: str, time: float, children=()) -> dict[str, Any]:
    return {"file_path_short": path, "function": function, "time": time, "children": list(children)}


def test_split_does_not_charge_the_loop_driver() -> None:
    """`base_events._run_once` holds the whole wait whether or not the wait
    was attributed correctly, because the awaiting frame is nested under it.
    Charging it makes a correct profile read as a broken one."""
    tree = _frame(
        "asyncio/base_events.py",
        "_run_once",
        0.30,
        [
            _frame("tests/test_profile.py", "waits_here", 0.297),
            _frame("selectors.py", "select", 0.003),
        ],
    )

    selector, awaiting = _split(_FakeProfiler(tree))  # type: ignore[arg-type]

    assert awaiting == 0.297
    assert selector == 0.003, "only the selector's own time counts, not its ancestor's"


def test_split_still_sees_a_swallowed_wait() -> None:
    """The other direction, so the fix above cannot pass by charging nothing."""
    tree = _frame("asyncio/base_events.py", "_run_once", 0.30, [_frame("selectors.py", "select", 0.30)])

    selector, awaiting = _split(_FakeProfiler(tree))  # type: ignore[arg-type]

    assert selector == 0.30
    assert awaiting == 0


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
    # A share, not exact zero. The loop really does enter `select` between
    # wakeups, and a 1 ms sampler catches 1-4 of those samples. Measured
    # worst share over 49 runs: 3.6%. The failure this guards against puts
    # the *whole* wait in the selector, which is 100%.
    assert selector < awaiting / 10, f"{selector}s of {awaiting}s landed in the loop's selector, not the awaiting frame"


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
    # A share, for the same reason as the other arm: the sampler catches a
    # stray frame on the way into and out of the await. Measured 0.000824s
    # against a 0.05s wait, 1.6%, on a fully loaded machine.
    assert awaiting < selector / 10, (
        f"{awaiting}s of {selector}s reached the awaiting frame, which this placement cannot attribute"
    )
