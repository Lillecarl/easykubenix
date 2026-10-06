"""Select an attribute path the way Nix does, with errors that name what is there.

Moved here from nanopynix_helpers' `eval_target`, onto huggorm. A huggorm
value reads `has` and `get` only once it is forced, so every step forces
through the state first.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from huggorm.errors import NixTypeError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from huggorm import AsyncEvalState, AsyncValue


class EvaluationTargetError(RuntimeError):
    """An evaluation target or attribute selection is invalid."""


_MAX_SUGGESTED_ATTRS = 10


def parse_attr_path(attrpath: str) -> tuple[str, ...]:
    """Split *attrpath* into components, the way Nix splits it.

    This is `parseAttrPath` of `src/libexpr/attr-path.cc`. A component may
    carry quotation marks, so ``packages."x86_64-linux".hello`` is three
    components and not four. That form is the only way to name an attribute
    that holds a dot.

    An empty string gives no components, and selecting no component returns
    the value itself. That is what the ``#.`` form of a fragment asks for.
    """
    parts: list[str] = []
    current = ""
    index = 0
    length = len(attrpath)
    while index < length:
        char = attrpath[index]
        if char == ".":
            parts.append(current)
            current = ""
        elif char == '"':
            index += 1
            while True:
                if index == length:
                    raise EvaluationTargetError(f"missing closing quote in selection path {attrpath!r}")
                if attrpath[index] == '"':
                    break
                current += attrpath[index]
                index += 1
        else:
            current += char
        index += 1
    # A trailing separator adds nothing, because Nix appends the last
    # component only when it holds a character. `a.` is one component.
    if current:
        parts.append(current)
    return tuple(parts)


class AttrPathNotFoundError(EvaluationTargetError):
    """One attribute path did not resolve.

    *depth* is the number of components that did resolve, so a caller that
    tries several candidates can report the one that went furthest.
    """

    def __init__(self, message: str, *, depth: int, available: Sequence[str]) -> None:
        super().__init__(message)
        self.depth = depth
        self.available = tuple(available)


def _describe(names: Sequence[str]) -> str:
    listed = ", ".join(names[:_MAX_SUGGESTED_ATTRS])
    suffix = "" if len(names) <= _MAX_SUGGESTED_ATTRS else f", ... ({len(names)} total)"
    return f"{listed}{suffix}"


async def select_attr_path(state: AsyncEvalState, value: AsyncValue, parts: Sequence[str]) -> AsyncValue:
    """Select each component of *parts* in turn.

    Raises :class:`AttrPathNotFoundError` when a component is absent, and also when
    a component asks for an attribute of a value that is not an attribute set.
    Nix treats those two the same way: `InstallableFlake::getCursors` catches
    the type error and moves to the next candidate.
    """
    for depth, part in enumerate(parts):
        if not part:
            raise AttrPathNotFoundError("attribute path contains an empty component", depth=depth, available=())
        await state.force(value)
        try:
            present = await value.has(part)
        except NixTypeError as exc:
            raise AttrPathNotFoundError(str(exc), depth=depth, available=()) from exc
        if not present:
            names = await value.names()
            raise AttrPathNotFoundError(
                f"attribute {part!r} not found; available attributes: {_describe(names)}",
                depth=depth,
                available=names,
            )
        value = await value.get(part)
    return value


async def select_attr(state: AsyncEvalState, value: AsyncValue, attrpath: str) -> AsyncValue:
    """Select one attribute path, with useful missing-attribute errors."""
    return await select_attr_path(state, value, parse_attr_path(attrpath))


async def select(state: AsyncEvalState, value: AsyncValue, *parts: str) -> AsyncValue:
    """Select literal components, such as ``"kubernetes", "generated"``."""
    return await select_attr_path(state, value, parts)
