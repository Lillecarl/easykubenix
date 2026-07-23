{
  mkShell,
  python,
  sphinx,
  myst-parser,
  furo,
  pytest,
  anyio,
  ruff,
  pyright,
  sops,
  age,
}:
let
  pythonEnv = python.withPackages (
    pp: [
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
    ruff
    pyright
    # For local key management/testing (age-keygen etc) and manual sops
    # inspection -- ekn's own sops.py (now in nanopynix) shells out to the
    # real `sops` CLI too, but these aren't needed to import that code.
    sops
    age
  ];
}
