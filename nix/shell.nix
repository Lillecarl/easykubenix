{
  mkShell,
  python,
  sphinx,
  myst-parser,
  furo,
  pytest,
  anyio,
  pygit2,
  pyyaml,
  typer,
}:
let
  pythonEnv = python.withPackages (pp: [
    sphinx
    myst-parser
    furo
    pytest
    anyio
    pygit2
    pyyaml
    typer
  ]);
in
mkShell {
  packages = [ pythonEnv ];
}
