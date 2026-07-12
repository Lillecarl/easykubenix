from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Optional

import typer

from ekn.eval import NixError, evaluate_file

app = typer.Typer(
    name="ekn",
    help="easykubenix CLI — evaluate Nix and dump JSON.",
    no_args_is_help=True,
    add_completion=True,
)


@app.callback(invoke_without_command=True)
def main(
    file: Path = typer.Option(
        ...,
        "--file",
        "-f",
        help="Nix file to evaluate.",
        exists=True,
        dir_okay=False,
        readable=True,
    ),
    attr_path: Optional[str] = typer.Argument(
        None,
        help="Dot-path into the evaluated file (e.g. 'a.b.c'). Omit to deep-force the whole file.",
    ),
) -> None:
    try:
        result = asyncio.run(evaluate_file(file, attr_path))
    except NixError as exc:
        typer.echo(exc.msg_without_ansi, err=True)
        raise typer.Exit(code=1)
    json.dump(result, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    app()
