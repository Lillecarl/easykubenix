{
  mkShell,
  python,
  sphinx,
  myst-parser,
  furo,
  pytest,
  anyio,
  ekn,
  helmrpc,
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
  packages = [
    pythonEnv
    helmrpc
    ruff
    pyright
  ];
}
