{
  mkShell,
  python,
  sphinx,
  myst-parser,
  furo,
  pytest,
  anyio,
  ekn,
  ruff,
  pyright,
}:
let
  pythonEnv = python.withPackages (
    pp:
    ekn.dependencies
    ++ [
      sphinx
      myst-parser
      furo
      pytest
      anyio
    ]
  );
in
mkShell {
  packages = [ pythonEnv ruff pyright ];
}
