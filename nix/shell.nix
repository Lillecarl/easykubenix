{
  mkShell,
  python,
  sphinx,
  myst-parser,
  furo,
}:
let
  pythonEnv = python.withPackages (pp: [
    sphinx
    myst-parser
    furo
  ]);
in
mkShell {
  packages = [ pythonEnv ];
}
