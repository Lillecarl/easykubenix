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
  sops,
  age,
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
    ruff
    pyright
    # `ekn/src/ekn/sops.py`'s decrypt-on-apply step shells out to the real
    # `sops` CLI (never reimplements its crypto/file format); `age` is here
    # for local key management/testing (age-keygen etc), not imported by
    # any ekn code itself.
    sops
    age
  ];
}
