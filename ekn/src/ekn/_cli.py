"""The declaration of an `ekn` command, and the argparse parser it becomes.

**A command class declares its options once, and the parser is built from it.**
Every `run` body in `ekn.cli` reads its options off the command object --
`self.flake`, `self.attr`, `self.push` -- so a decorator-driven parser would
declare each option twice: once for the parser, and once for the class that
carries it into the body. This layer declares them once, and the shape of a
command does not change::

    class Render(Command):
        \"\"\"Render Kubernetes manifests as YAML on stdout.\"\"\"

        attr: str | None = opt(None, short="A", help="...")

        async def run(self) -> None:
            ...

**argparse parses, and argcomplete completes.** This replaces clypi, which
published no way to reach its completion script and resolved the shell through
`os.environ["SHELL"]`. argcomplete gets the raw command line and the cursor
offset from the shell, so a value that holds `:` or `=` survives -- which is
what `--flake .#app<TAB>` and `--attr=kube<TAB>` need. nanopynix measured the
candidates on a pty in bash, zsh and fish, and picked it for that; `ekn` and
`pynix` now use the same protocol.

**A copy of nanopynix' `pynix._cli`, and not an import of it.** `ekn` depends on
`nanopynix`, not on `pynix`: `pynix` is a separate project in that repository
and is not in this program's closure. The two files are small and they answer
the same question, so each repository keeps its own for now. nanopynix#222 asks
for the layer as a library, which is what would delete this file.

**`argparse.SUPPRESS` is what says the caller named an option.** An option that
nobody named is absent from the namespace, and `Command.__init__` fills it from
the declaration. clypi needed an `UNSET` sentinel for that; it is gone.
"""

from __future__ import annotations

import argparse
import inspect
import os
import types
import typing
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, NoReturn

import argcomplete

#: What argcomplete calls a completer: it is handed the text typed so far and
#: answers with the candidates that start with it.
type Completer = Callable[..., Sequence[str]]

#: The default of a positional that the caller must give. Not `None`, which is
#: a legal default for an optional one.
MISSING: typing.Final = object()


@dataclass(frozen=True)
class Spec:
    """One declared option or positional, before argparse has seen it."""

    #: What the caller gets when they name neither the flag nor a value.
    default: Any = None
    #: The help line. argparse wraps it, so it is written as one sentence.
    help: str = ""
    #: A one-letter alias, as `-A`. Options only.
    short: str | None = None
    #: True for a flag that also gets a `--no-` spelling. A `bool` that
    #: defaults to True needs this, or the caller can never turn it off.
    negatable: bool = False
    #: True for an option the caller must name.
    required: bool = False
    #: True for a positional argument.
    positional: bool = False
    #: What answers a Tab after this option. `None` leaves it to argcomplete,
    #: which offers file names.
    complete: Completer | None = None


def opt(  # noqa: PLR0913 -- one keyword for each thing a declaration can say; a dict would hide the names from the reader and from pyright
    default: Any = None,
    *,
    help: str,  # noqa: A002 -- argparse names the parameter `help`, and so did clypi
    short: str | None = None,
    negatable: bool = False,
    required: bool = False,
    complete: Completer | None = None,
) -> Any:
    """Declare an option."""
    return Spec(
        default=default,
        help=help,
        short=short,
        negatable=negatable,
        required=required,
        complete=complete,
    )


def pos(*, help: str, default: Any = MISSING, complete: Completer | None = None) -> Any:  # noqa: A002 -- see opt
    """Declare a positional argument."""
    return Spec(default=default, help=help, positional=True, complete=complete)


class Command:
    """The base of every `ekn` command.

    A subclass declares its options as annotated class attributes and gets an
    `__init__` that takes them as keyword arguments. `run` is what the command
    does, and `build_parser` is what makes it reachable.
    """

    #: The name on the command line. Defaults to the class name in kebab case,
    #: so only a command that spells itself differently -- `kubeapply`,
    #: `_yamlToJson` -- has to say it.
    cli_name: ClassVar[str] = ""

    #: The commands mounted under this one. Empty for a leaf.
    subcommands: ClassVar[tuple[type[Command], ...]] = ()

    #: Filled by `__init_subclass__`, so a subclass of a subclass inherits the
    #: options of its base. `Deploy(Commit)` gets every option of `Commit`
    #: without redeclaring one of them -- clypi parsed only what the class
    #: itself declared, which is why they used to be written twice.
    specs: ClassVar[dict[str, Spec]] = {}

    #: The annotation of each declared name, resolved to an object. It decides
    #: whether an option is a flag, a path, a number or a fixed set of words.
    types: ClassVar[dict[str, Any]] = {}

    #: The parser the caller was dispatched through, for a command that prints
    #: its own help. `dispatch` sets it; a command built by hand has none.
    parser: ClassVar[argparse.ArgumentParser | None] = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        specs: dict[str, Spec] = {}
        annotations: dict[str, Any] = {}
        for base in reversed(cls.__mro__):
            if base is object:
                continue
            resolved = inspect.get_annotations(base, eval_str=True)
            for field, value in vars(base).items():
                if isinstance(value, Spec):
                    specs[field] = value
                    annotations[field] = resolved.get(field, str)
        shadowed = sorted(specs.keys() & _RESERVED)
        if shadowed:
            raise TypeError(f"{cls.__name__} declares {shadowed}, which this class already uses for itself")
        cls.specs = specs
        cls.types = annotations

    def __init__(self, **values: Any) -> None:
        """Take what the caller named, and fill the rest from the declaration.

        Every option is declared with `argparse.SUPPRESS`, so *values* holds
        exactly what the caller typed.
        """
        for field, spec in type(self).specs.items():
            if field in values:
                setattr(self, field, values[field])
            else:
                setattr(self, field, _declared_default(spec, type(self).types[field]))

    def print_help(self) -> NoReturn:
        """Print the help of this command, and exit.

        stdout, because the caller asked for it. argparse writes a usage error
        to stderr on its own, so nothing here has to redirect anything -- which
        is what `EknCommand.print_help` existed for under clypi.
        """
        parser = type(self).parser or build_parser(type(self))
        parser.print_help()
        raise SystemExit(0)

    async def run(self) -> None:
        """What the command does."""
        raise NotImplementedError(type(self).__name__)


#: The annotations that argparse has to be told about. Everything else it
#: leaves as the string the caller typed, which is what `str` wants anyway.
_CONVERTED: typing.Final = (int, float, Path)

#: The class attributes this layer owns. A command that declares an option of
#: the same name would shadow one of them, so `__init_subclass__` refuses.
_RESERVED: typing.Final = frozenset({"cli_name", "subcommands", "specs", "types", "parser", "print_help", "run"})


def _declared_default(spec: Spec, annotation: Any) -> Any:
    """The value of an option the caller did not name.

    A repeated option gets a new list each time. A shared one would be the
    default of every instance and would keep what an earlier run appended.
    """
    if spec.default in {None, MISSING} and typing.get_origin(_unwrapped(annotation)) is list:
        return []
    return None if spec.default is MISSING else spec.default


def _unwrapped(annotation: Any) -> Any:
    """*annotation* with `| None` and any alias taken off.

    A `type X = ...` alias is an object of its own, and `get_origin` says
    nothing about what it stands for. `ekn.cli`'s `LogLevel` is one, so this
    reads through it to the `Literal` underneath.
    """
    if isinstance(annotation, typing.TypeAliasType):
        return _unwrapped(annotation.__value__)
    if typing.get_origin(annotation) in {typing.Union, types.UnionType}:
        parts = [part for part in typing.get_args(annotation) if part is not type(None)]
        if len(parts) == 1:
            return parts[0]
    return annotation


def _flags(field: str, spec: Spec) -> list[str]:
    """The flag spellings of *field*, longest first, as argparse wants them."""
    flags = ["--" + field.replace("_", "-")]
    if spec.short is not None:
        flags.append("-" + spec.short.lstrip("-"))
    return flags


def _add(parser: argparse.ArgumentParser, field: str, spec: Spec, annotation: Any) -> None:
    """Add one declared option or positional to *parser*."""
    inner = _unwrapped(annotation)
    kwargs = _positional_kwargs(spec, inner) if spec.positional else _option_kwargs(spec, inner)
    kwargs["help"] = spec.help
    names = [field] if spec.positional else _flags(field, spec)
    action = parser.add_argument(*names, **kwargs)
    # argcomplete reads this attribute off the action it did not create. `None`
    # leaves the default completer, which offers file names.
    action.completer = spec.complete  # type: ignore[attr-defined]


def _positional_kwargs(spec: Spec, inner: Any) -> dict[str, Any]:
    """What `add_argument` needs for one declared positional."""
    if typing.get_origin(inner) is list:
        return {"nargs": "*"}
    kwargs: dict[str, Any] = {}
    if spec.default is not MISSING:
        kwargs["nargs"] = "?"
        kwargs["default"] = argparse.SUPPRESS
    if inner in _CONVERTED:
        kwargs["type"] = inner
    return kwargs


def _option_kwargs(spec: Spec, inner: Any) -> dict[str, Any]:
    """What `add_argument` needs for one declared option.

    **`SUPPRESS`, for every one of them.** An option the caller did not name is
    then absent from the namespace, and `Command.__init__` fills it from the
    declaration. A subcommand therefore never overwrites a value the caller
    gave the root -- `ekn --flake .#app render` reaches `render` with it.
    """
    kwargs: dict[str, Any] = {"default": argparse.SUPPRESS}
    if spec.required:
        kwargs["required"] = True
    if spec.negatable:
        kwargs["action"] = argparse.BooleanOptionalAction
    elif inner is bool:
        kwargs["action"] = "store_true"
    elif typing.get_origin(inner) is list:
        kwargs["action"] = "append"
    elif typing.get_origin(inner) is typing.Literal:
        # A fixed set of words. argparse rejects anything else, and argcomplete
        # offers the set on Tab.
        kwargs["choices"] = typing.get_args(inner)
    elif inner in _CONVERTED:
        # **argparse hands back a string unless it is told otherwise.** Without
        # this, `--steps-back 2` reaches `rollback_branches` as `"2"`.
        kwargs["type"] = inner
    return kwargs


def command_name(command: type[Command]) -> str:
    """The name of *command* on the command line."""
    if command.cli_name:
        return command.cli_name
    head, *rest = command.__name__
    return head.lower() + "".join("-" + c.lower() if c.isupper() else c for c in rest)


def _describe(command: type[Command]) -> str:
    """The help text of *command*, which is its docstring."""
    return inspect.getdoc(command) or ""


def _summary(command: type[Command]) -> str:
    """The one line `ekn --help` prints beside the name of *command*.

    The first paragraph of the docstring, as one line. Not the first *line*:
    these docstrings are wrapped, so a first line cuts a sentence in half
    ("Verify, push the pre-deploy cache, commit, and push -- the whole").
    argparse wraps what it gets, so the length is its problem.
    """
    return " ".join(_describe(command).partition("\n\n")[0].split())


def _configure(parser: argparse.ArgumentParser, command: type[Command]) -> None:
    """Put the options of *command* on *parser*, and mount what is under it."""
    for field, spec in command.specs.items():
        if not spec.positional:
            _add(parser, field, spec, command.types[field])
    for field, spec in command.specs.items():
        if spec.positional:
            _add(parser, field, spec, command.types[field])
    # `set_defaults`, so that the parser of the deepest command names the class
    # that runs. argparse hands the namespace back with no record of which
    # subparser filled it in, and a subparser's defaults overwrite the root's.
    parser.set_defaults(_command=command)
    if not command.subcommands:
        return
    sub = parser.add_subparsers(metavar="COMMAND")
    for child in command.subcommands:
        _configure(
            sub.add_parser(
                command_name(child),
                help=_summary(child),
                description=_describe(child),
            ),
            child,
        )


def build_parser(root: type[Command]) -> argparse.ArgumentParser:
    """*root*, and everything under it, as an argparse parser."""
    parser = argparse.ArgumentParser(prog=command_name(root), description=_describe(root))
    _configure(parser, root)
    return parser


#: Names that argcomplete must not offer. `-h` and `--help` are argparse's own
#: and are noise beside the real candidates.
_NOT_OFFERED: typing.Final = ("-h", "--help")


def complete(parser: argparse.ArgumentParser, **kwargs: Any) -> None:
    """Answer a shell completion, when this start is one, and exit.

    `_ARGCOMPLETE` is set by the generated script and by nothing else, so this
    returns at once on a real command. It does not return on a completion: it
    writes the candidates to the file descriptor the script opened, and exits.

    *kwargs* reach `argcomplete.autocomplete`. `tests/test_cli_completion.py`
    passes `output_stream` and `exit_method` there, which is what lets it read
    what a real Tab press answers without a shell.
    """
    if not os.environ.get("_ARGCOMPLETE"):
        return
    argcomplete.autocomplete(parser, exclude=_NOT_OFFERED, **kwargs)


def dispatch(parser: argparse.ArgumentParser, namespace: argparse.Namespace) -> Command:
    """The command the caller named, built from what they typed."""
    values = {name: value for name, value in vars(namespace).items() if not name.startswith("_")}
    # `_command` is the parser's own record of which subparser parsed, put
    # there by `_configure`. The leading underscore is what keeps it out of
    # `values` above.
    command: type[Command] = namespace._command
    command.parser = parser
    return command(**values)
