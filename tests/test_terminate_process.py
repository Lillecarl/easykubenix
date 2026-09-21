"""`terminate_process`, which is what stops `ekn validate` leaking a cluster.

`EphemeralControlPlane` holds etcd and kube-apiserver in attributes rather
than in an `async with` block, because the pair has to outlive the method that
starts it. So nothing but its own teardown ends them, and the teardown runs on
the two paths where that is hardest: a start that failed halfway, and a
Ctrl-C.

Real processes. The whole question is what a child does when it is signalled,
and a stand-in for one answers it by assumption.
"""

from __future__ import annotations

import subprocess
import sys

import anyio
import anyio.abc
import pytest

from ekn.validation import drain, terminate_process

#: Long enough that nothing here can end on its own and pass by accident.
FOREVER = "import time; time.sleep(3600)"

#: `print` first, so a test can wait until the handler is really installed --
#: a SIGTERM that arrives before it lands would kill the process and prove
#: nothing.
DEAF = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('up', flush=True); time.sleep(3600)"


async def spawn(program: str) -> anyio.abc.Process:
    return await anyio.open_process(
        [sys.executable, "-c", program],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )


async def up(process: anyio.abc.Process) -> None:
    """Wait for the child's first line of output."""
    assert process.stdout is not None
    assert await process.stdout.receive() == b"up\n"


class TestTerminateProcess:
    async def test_a_running_child_is_ended(self) -> None:
        process = await spawn(FOREVER)

        with anyio.fail_after(10):
            await terminate_process(process)

        assert process.returncode is not None

    async def test_a_child_that_ignores_sigterm_is_killed(self) -> None:
        """`terminate` alone is not enough, and waiting for one that will
        never answer is a hang. The grace period is what turns the second
        into the third."""
        process = await spawn(DEAF)
        await up(process)

        with anyio.fail_after(10):
            await terminate_process(process, grace=0.2)

        assert process.returncode == -9

    async def test_an_already_dead_child_is_not_signalled_again(self) -> None:
        process = await spawn("pass")
        await process.wait()

        with anyio.fail_after(10):
            await terminate_process(process)

        assert process.returncode == 0

    async def test_a_cancelled_caller_still_gets_a_dead_child(self) -> None:
        """The case the shield exists for. Ctrl-C reaches this program as a
        cancellation, and `EphemeralControlPlane.__aexit__` tears down from
        inside the scope that was cancelled.

        Without the shield the `await process.wait()` below is cancelled the
        instant it starts, the cancellation leaves `terminate_process`, and
        `move_on_after` swallows it here -- so this test reads as a pass while
        etcd and kube-apiserver are still running. `returncode` is what tells
        the two apart: it is set only by a wait that finished.
        """
        process = await spawn(FOREVER)

        with anyio.move_on_after(0.01):
            try:
                await anyio.sleep(3600)
            finally:
                await terminate_process(process)

        assert process.returncode is not None

    async def test_an_unshielded_wait_is_what_that_test_would_catch(self) -> None:
        """The negative control, written out rather than described: the same
        teardown without the shield, under the same cancellation."""
        process = await spawn(FOREVER)

        with anyio.move_on_after(0.01):
            try:
                await anyio.sleep(3600)
            finally:
                process.terminate()
                with pytest.raises(anyio.get_cancelled_exc_class()):
                    await process.wait()

        assert process.returncode is None, "an unshielded wait reported an exit it never saw"

        with anyio.fail_after(10):
            await terminate_process(process)


class TestDrain:
    async def test_it_returns_everything_the_child_wrote(self) -> None:
        process = await spawn("print('one'); print('two')")
        await process.wait()

        assert await drain(process.stdout) == "one\ntwo\n"
        await terminate_process(process)

    async def test_no_stream_is_an_empty_string(self) -> None:
        """`stderr` is `None` for a child started with `DEVNULL`, and the
        caller reports it without checking."""
        assert await drain(None) == ""
