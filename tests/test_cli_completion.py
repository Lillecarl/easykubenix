"""What `ekn` answers when a caller presses Tab.

**The real parser, and the real library.** `ekn._cli.complete` is what `main`
calls, and this drives that function -- not a copy of the table it builds. So a
command that is renamed, an option that is added and a `Literal` that grows a
member all reach this file on their own.

**No shell here.** argcomplete's protocol is environment variables in and bytes
out: the shell sends the whole command line in `COMP_LINE` and the cursor
offset in `COMP_POINT`, and the answer goes to a file descriptor the generated
script opened, separated by `_ARGCOMPLETE_IFS`. `output_stream` and
`exit_method` are argcomplete's own seams for reading that answer in-process.
`nix/default.nix`'s `ekn-completions` gate covers the other half, which is that
the built package installs the scripts a shell loads.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, override

import argcomplete
import pytest

from ekn._cli import build_parser, complete
from ekn.cli import Ekn

if TYPE_CHECKING:
    from collections.abc import Sequence


class QuietFinder(argcomplete.CompletionFinder):
    """The library's own finder, with its debug stream left alone.

    argcomplete opens file descriptor 9 for debug output, and pytest is already
    using that descriptor. `CompletionFinder._init_debug_stream` documents
    itself as the place to override for exactly this case. The stream defaults
    to stderr and nothing writes to it unless `_ARC_DEBUG` is set, so leaving
    it costs the test nothing.
    """

    @override
    def _init_debug_stream(self) -> None:
        return


#: What argcomplete puts between two candidates. The generated script sets it,
#: so a test that speaks the protocol has to set it too.
IFS = "\013"

#: Every subcommand of `ekn`, as the parser spells it. `_yamlToJson` and
#: friends are here because a completion offers them too -- they are internal
#: by documentation, not by hiding.
SUBCOMMANDS = frozenset(
    {
        "deploy",
        "eval",
        "render",
        "diff",
        "commit",
        "rollback",
        "validate",
        "kubeapply",
        "clusterdiff",
        "pushcache",
        "split-manifest",
        "_applyManifest",
        "_yamlToJson",
        "_jsonToYAML",
    }
)


def candidates(line: str, monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """What a shell would offer for *line*, with the cursor at its end."""
    monkeypatch.setattr(argcomplete, "autocomplete", QuietFinder())
    monkeypatch.setenv("_ARGCOMPLETE", "1")
    monkeypatch.setenv("_ARGCOMPLETE_IFS", IFS)
    monkeypatch.setenv("_ARGCOMPLETE_COMP_WORDBREAKS", " \t\n\"'><=;|&(:")
    monkeypatch.setenv("COMP_LINE", line)
    monkeypatch.setenv("COMP_POINT", str(len(line)))
    monkeypatch.setenv("COMP_TYPE", "9")
    answer = io.StringIO()
    codes: list[int] = []
    complete(build_parser(Ekn), output_stream=answer, exit_method=codes.append)
    assert codes == [0], f"argcomplete exited with {codes}"
    return {candidate for candidate in answer.getvalue().split(IFS) if candidate}


def test_a_real_command_line_is_not_a_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    """`complete` returns at once when `_ARGCOMPLETE` is unset.

    Every `ekn` run reaches it, and only a Tab press has that variable.
    """
    monkeypatch.delenv("_ARGCOMPLETE", raising=False)
    calls: list[object] = []
    complete(build_parser(Ekn), exit_method=calls.append)
    assert calls == []


def test_an_empty_line_offers_every_subcommand(monkeypatch: pytest.MonkeyPatch) -> None:
    assert candidates("ekn ", monkeypatch) >= SUBCOMMANDS


def test_help_is_never_offered(monkeypatch: pytest.MonkeyPatch) -> None:
    """`-h` and `--help` are argparse's own, and are noise beside the rest.

    `_cli._NOT_OFFERED` is what excludes them.
    """
    offered = candidates("ekn -", monkeypatch)
    assert offered, "a lone dash offers nothing at all"
    assert "-h" not in offered
    assert "--help" not in offered


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # **The trailing space is part of the answer.** argcomplete appends one
        # to a match that is the only one, so the caller can type the next word
        # straight away. A row with more than one candidate gets none.
        ("ekn depl", ["deploy "]),
        # An underscore is an ordinary character here, so the internal commands
        # complete like the rest.
        ("ekn _yaml", ["_yamlToJson "]),
        # Both spellings of a negatable flag. `argparse.BooleanOptionalAction`
        # writes them from the one declaration, and a caller can type either.
        # `--substitute-on-destination` defaults to True, so the `--no-` half
        # is the only way to turn it off.
        ("ekn pushcache --substitute", ["--substitute-on-destination "]),
        ("ekn pushcache --no-", ["--no-substitute-on-destination "]),
        # A `Literal` becomes `choices`, and the shell offers the members.
        ("ekn _yamlToJson --yaml-version ", ["yaml11", "yaml12"]),
        ("ekn deploy --verbosity t", ["talkative "]),
        # A quoted flake reference. The word before the cursor is what is being
        # completed, and the reference in front of it is a value like any other.
        ('ekn --flake ".#app" ren', ["render "]),
    ],
)
def test_a_line_offers_what_it_should(line: str, expected: Sequence[str], monkeypatch: pytest.MonkeyPatch) -> None:
    assert candidates(line, monkeypatch) == set(expected)


@pytest.mark.xfail(
    strict=True, reason="argcomplete lexes `#` as the start of a comment; bash does not -- nanopynix#221"
)
def test_an_unquoted_flake_reference_does_not_stop_the_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    """**The one line this program is typed with that answers wrongly.**

    `ekn --flake .#app ren<TAB>` should offer `render` and offers every
    subcommand instead. argcomplete lexes the line with `shlex`, which treats
    `#` as the start of a comment wherever it stands, so it never sees `ren` at
    all -- it completes an empty word after `--flake .`. bash itself treats `#`
    as a comment only at the start of a word, so the shell disagrees with the
    library here.

    Quoting the reference is the way around it, and the row above holds that
    line. The mark is `strict`, so this test turns red the day argcomplete
    stops truncating and someone has to delete it.

    nanopynix#221 tracks the fix. It is one line in `argcomplete.lexers`:
    `lexer.commenters = ""`, because a command line is not a script.
    """
    assert candidates("ekn --flake .#app ren", monkeypatch) == {"render "}
