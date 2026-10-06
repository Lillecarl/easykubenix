/**
  jsonschema-rs, newer than nixpkgs carries, from Lillecarl's fork: the
  python-v0.58.6 tag plus `validate_schema`, which Kubernetes' RE2 patterns
  need (Lillecarl/jsonschema branch validate-schema-option).

  The repository tracks no Cargo.lock. `./Cargo.lock` is the PyPI sdist's for
  the same tag, and cargo resolves the whole GitHub workspace from it
  unchanged.

  No tests: upstream runs them, and ekn's own tests cover what ekn relies on.
*/
{
  rustPlatform,
  fetchFromGitHub,
  jsonschema-rs,
}:
jsonschema-rs.overridePythonAttrs (_: {
  version = "0.58.6";
  src = fetchFromGitHub {
    owner = "Lillecarl";
    repo = "jsonschema";
    rev = "148a30c5e6ab81e112498a576c530b127870d0cf";
    hash = "sha256-QpymROXHN7VYS3mimHHUCdBROD7YRA++/OFUL5SCpD0=";
  };
  cargoDeps = rustPlatform.importCargoLock { lockFile = ./Cargo.lock; };
  # Set, not appended: easykubenix' overlay can reach one package set twice.
  postPatch = ''
    ln -s ${./Cargo.lock} Cargo.lock
  '';
  buildAndTestSubdir = "crates/jsonschema-py";
  doCheck = false;
})
