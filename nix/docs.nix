{
  lib,
  stdenvNoCC,
  python,
  sphinx,
  myst-parser,
  furo,
}:
let
  pythonEnv = python.withPackages (_: [
    sphinx
    myst-parser
    furo
  ]);
in
stdenvNoCC.mkDerivation {
  pname = "easykubenix-docs";
  version = "0";

  src = lib.cleanSource ../.;

  nativeBuildInputs = [ pythonEnv ];

  buildPhase = ''
    runHook preBuild
    EASYKUBENIX_DOCS_OFFLINE=1 sphinx-build -b html docs "$out" -W
    runHook postBuild
  '';

  dontInstall = true;
}
