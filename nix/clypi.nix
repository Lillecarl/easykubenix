{
  lib,
  fetchFromGitHub,
  buildPythonPackage,
  hatchling,
  python-dateutil,
  typing-extensions,
  pytestCheckHook,
  anyio,
  types-python-dateutil,
}:
buildPythonPackage (finalAttrs: {
  pname = "clypi";
  version = "1.9.0";
  pyproject = true;
  src = fetchFromGitHub {
    owner = "danimelchor";
    repo = "clypi";
    rev = finalAttrs.version;
    hash = "sha256-FFTeuWyp/ZtWu8JxuZ68FfNB8vZetcQ8Y1TvYYsU/4k=";
  };
  build-system = [ hatchling ];
  dependencies = [
    python-dateutil
    typing-extensions
  ];
  postPatch = ''
    substituteInPlace pyproject.toml --replace-fail 'hatchling==1.27.0' 'hatchling>=1.27.0'
  '';
  nativeCheckInputs = [
    pytestCheckHook
    anyio
    types-python-dateutil
  ];
  meta = {
    description = "Your all-in-one for beautiful, lightweight, prod-ready CLIs";
    homepage = "https://danimelchor.github.io/clypi/";
    license = lib.licenses.mit;
    maintainers = [ lib.maintainers.lillecarl ];
  };
})
