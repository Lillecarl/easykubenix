{
  mkShell,
  python,
  sphinx,
  myst-parser,
  furo,
  ruff,
  pyright,
  nixfmt,
  sops,
  age,
  # This repository's own venv -- see `eknDevEnv` in ../default.nix. One
  # environment holding `ekn`'s whole dependency closure (kr8s, argcomplete,
  # pygit2, pydantic, structlog, rich, pyyaml, anyio) together with
  # `nanopynix` and its `test` extra, which is what brings pytest.
  #
  # `ekn`'s *source* lives in ../ekn, and pytest reads it from there via
  # `pythonpath` in ../pytest.ini rather than from this venv. What the venv
  # supplies is the dependency closure around it, which a hand-written
  # `python.withPackages` list could not: nixpkgs' Python environments keep
  # only importable modules and so drop an application together with
  # everything it propagates.
  eknDevEnv,
}:
let
  # Sphinx is only a command-line tool here. The docs are hand-written Markdown
  # with no autodoc, so `sphinx-build` does not import `ekn` or anything else
  # from this repository -- it needs no overlap with `eknDevEnv`.
  docsEnv = python.withPackages (pp: [
    sphinx
    myst-parser
    furo
  ]);
in
mkShell {
  # The order of this list decides the order of PATH. `eknDevEnv` must come
  # first. Both environments give a `python3`, and pytest must run in the one
  # that holds `ekn` and `nanopynix`. `docsEnv` still gives `sphinx-build`,
  # because `eknDevEnv` does not have it.
  packages = [
    eknDevEnv
    docsEnv
    ruff
    pyright
    # Same version the `nixfmt` gate in ./default.nix runs, because both come
    # from this repository's nixpkgs pin. A formatter that disagrees with its
    # own gate is worse than none, and nixfmt's output does move between
    # releases.
    nixfmt
    # For local key management/testing (age-keygen etc) and manual sops
    # inspection. ../ekn/src/ekn/sops.py shells out to the real `sops` CLI, so
    # the tests that exercise it need one on PATH.
    sops
    age
  ];
}
