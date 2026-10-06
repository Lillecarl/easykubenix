"""ekn's evaluator, in-process on huggorm.

One `evaluator()` block is one store, one evaluator on it, the YAML builtins,
and Nix's log routed to stderr the way `ekn` wants it. `Value` keeps the lazy
`.attr()` chain `eval.py` is written in: nothing is selected until something
reads the value.

In-process, not in a worker. Nix is stable, and a worker pays only for
massively parallel evaluation (easykubenix#52).
"""

from __future__ import annotations

import json
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Any

from huggorm import (
    AsyncSession,
    StorePath,
    enable_experimental_feature,
    filter_ansi_escapes,
    load_config,
    parse_flake_ref,
    set_default_verbosity,
)

from ekn.attrpath import parse_attr_path, select_attr_path
from ekn.nixyaml import register_yaml_primops
from ekn.storecheck import STORE_DIR

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from huggorm import AsyncEvalState, AsyncStore, AsyncValue, CapturedLogs, LogRecord
    from huggorm.jsonprimop import JsonValue

#: Nix's levels, by number: `nix::Verbosity`.
LEVELS = ("error", "warn", "notice", "info", "talkative", "chatty", "debug", "vomit")
_WARN = LEVELS.index("warn")

#: `nix::resBuildLogLine`: one line a builder printed, in `fields[0]`.
_RES_BUILD_LOG_LINE = 101

#: What `ekn` needs Nix to accept: the set its evaluator has always had.
EXPERIMENTAL_FEATURES = ("flakes", "nix-command", "ca-derivations", "dynamic-derivations", "recursive-nix")


@cache
def _configure_process() -> None:
    """Read nix.conf and enable the features, once per process.

    `load_config` first, so a feature the host's nix.conf leaves off is still
    enabled; the settings are process-wide, as `nix` itself has them.
    """
    load_config()
    for feature in EXPERIMENTAL_FEATURES:
        enable_experimental_feature(feature)


def store_path(path: str) -> StorePath:
    """A `/nix/store/...` string as a `StorePath`."""
    return StorePath(path.removeprefix(f"{STORE_DIR}/"))


def _plain(record: LogRecord) -> str:
    return filter_ansi_escapes(record.text(), filter_all=True)


def print_record(record: LogRecord) -> None:
    """Every message and build log line, as `--print-build-logs` asks."""
    if record.action() == "result" and record.type() == _RES_BUILD_LOG_LINE:
        fields = record.fields()
        if fields and not fields[0].is_int():
            line = fields[0].text()
            sys.stderr.write(line if line.endswith("\n") else line + "\n")
        return
    if record.action() == "msg" and (message := _plain(record)):
        sys.stderr.write(message + "\n")


def print_warning(record: LogRecord) -> None:
    """Warnings and errors only, and never gated on verbosity.

    A warning is the module system telling the user that their config needs
    attention -- `config.warnings`, a `mkRenamedOptionModule` notice -- and
    `nix build` on the same config prints it. Nix renders the prefix itself:
    "evaluation warning" for one an expression raised, "warning" for its own.
    """
    if record.action() == "msg" and record.level() <= _WARN and (message := _plain(record)):
        sys.stderr.write(message + "\n")


@dataclass(frozen=True)
class Value:
    """A Nix value, and an attribute path below it that is not selected yet."""

    state: AsyncEvalState
    base: AsyncValue
    path: tuple[str, ...] = ()

    def attr(self, name: str) -> Value:
        return Value(self.state, self.base, (*self.path, name))

    async def value(self) -> AsyncValue:
        """The value at the path, selected and forced."""
        selected = await select_attr_path(self.state, self.base, self.path)
        await self.state.force(selected)
        return selected

    async def select(self, attrpath: str) -> Value:
        """Select a Nix attribute path now, so a missing one raises here."""
        return Value(self.state, await select_attr_path(self.state, await self.value(), parse_attr_path(attrpath)))

    async def has_attr(self, name: str) -> bool:
        return await (await self.value()).has(name)

    async def to_python(self) -> JsonValue:
        """The value as plain data: Nix's own `printValueAsJSON`, strict."""
        return json.loads(await (await self.value()).to_json())

    async def realise_string(self) -> str:
        """The value as a string, with every store path it names built.

        A derivation coerces to its `outPath`, so this is also how a
        derivation's default output is built.
        """
        return await (await self.value()).realise_string()

    async def auto_call(self) -> Value:
        """Apply with no arguments, as Nix's `autoCallFunction` does.

        A lambda with formals gets its defaults, and a `__functor` is
        followed. Anything else answers as it is: huggorm's `apply_auto`
        refuses it, and a file that evaluates to an attribute set is the
        ordinary case here.
        """
        value = await self.value()
        if await value.type_name() == "attrs" and await value.has("__functor"):
            functor = await value.get("__functor")
            await self.state.force(functor)
            return await Value(self.state, await functor(value)).auto_call()
        if await value.type_name() == "function" and await value.is_lambda() and await value.has_formals():
            return Value(self.state, await value.apply_auto(await self.state.make_attrs()))
        return Value(self.state, value)


@dataclass(frozen=True)
class Evaluator:
    """One session, its default store, and one evaluator on that store."""

    session: AsyncSession
    store: AsyncStore
    state: AsyncEvalState

    async def file(self, path: str) -> Value:
        """A file's value, auto-called as `nix eval --file` does."""
        return await Value(self.state, await self.state.eval_file(path)).auto_call()

    async def flake(self, ref: str) -> Value:
        """A flake's outputs: locked, lock file written, outputs unforced."""
        locked = await self.state.lock_flake(parse_flake_ref(ref))
        try:
            return Value(self.state, await self.state.call_flake(locked))
        finally:
            await locked.aclose()

    async def expr(self, text: str) -> Value:
        return Value(self.state, await self.state.eval_expr(text))

    async def forget_files(self) -> None:
        """Forget every file this evaluator read, so the next read sees disk.

        Every file, not one. An importer keeps its cached answer when only
        the file it imports is forgotten.
        """
        for path in await self.state.cached_files():
            await self.state.forget_file(path)

    def capture(self) -> Any:
        """Collect what Nix says while the block runs: `CapturedLogs`."""
        return self.session.capture(self.state, level=_WARN)


@asynccontextmanager
async def session(*, verbosity: str = "warn") -> AsyncIterator[AsyncSession]:
    """A session for store work alone, such as a closure copy.

    Every store call runs on a pool thread, which keeps records at the
    default level: `verbosity`, never below warn.
    """
    _configure_process()
    set_default_verbosity(max(LEVELS.index(verbosity), _WARN))
    async with AsyncSession() as opened:
        yield opened


@asynccontextmanager
async def evaluator(
    *,
    verbosity: str = "warn",
    print_build_logs: bool = False,
    settings: Mapping[str, str] | None = None,
) -> AsyncIterator[Evaluator]:
    """Open an `Evaluator`, and route its log to stderr while it is open.

    The evaluator's own thread is subscribed, so nothing it says reaches
    stderr except what the printer writes: every message and build line with
    `print_build_logs`, warnings and errors without. A thread Nix starts for
    itself -- a substituter, a file transfer -- keeps records at the default
    level, which this sets to `verbosity` and never below warn.
    """
    _configure_process()
    level = max(LEVELS.index(verbosity), _WARN)
    set_default_verbosity(level)
    async with AsyncSession() as session:
        store = session.store()
        state = session.eval(store, dict(settings) if settings else None)
        await register_yaml_primops(state)
        printer = print_record if print_build_logs else print_warning
        async with session.forward(state, printer, level=level):
            yield Evaluator(session, store, state)


def captured_messages(logs: CapturedLogs) -> list[str]:
    """Each message of a capture, as plain text."""
    return [_plain(record) for record in logs.records if record.action() == "msg"]


__all__ = [
    "EXPERIMENTAL_FEATURES",
    "LEVELS",
    "Evaluator",
    "Value",
    "captured_messages",
    "evaluator",
    "print_record",
    "print_warning",
    "session",
    "store_path",
]
