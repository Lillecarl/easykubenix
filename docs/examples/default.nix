# The doc examples, and the checks over them, without flakes.
#
# This is where the examples are actually wired up. `flake.nix` next to it is a
# thin wrapper that calls this file, so there is one definition and the flake
# and non-flake paths cannot drift. Everything in this repository has to work
# from a plain `import`; a `flake.lock` is fine (nix/compat.nix reads it), a
# flake command being *required* is not.
#
# Build them with `nix build --file ./checks.nix all` from the repository root.
{
  inputs ? (import ../../nix/compat.nix).inputs,
  system ? builtins.currentSystem,
  pkgs ? inputs.nixpkgs.legacyPackages.${system},
  # The easykubenix entry point itself, as the function `default.nix` returns.
  # A default rather than a required argument, so this file stands on its own;
  # `flake.nix` passes its own `lib.easykubenix` instead, so that the examples
  # exercise exactly what the flake exports rather than a second import of it.
  easykubenix ? import ../../default.nix,
}:
let
  inherit (pkgs) lib;

  # The extended `pkgs`, whose `lib` also holds easykubenix' own helpers such
  # as `mkNamedList`. It is under `passthru`, not at the top level.
  eknPkgs =
    (easykubenix {
      inherit pkgs;
      modules = [ ];
    }).passthru.pkgs;

  names = [
    "basic"
    "namedlists"
    "helm"
    "generators"
    "edge-cases"
    "validation"
  ];

  examples = lib.genAttrs names (
    name:
    import ./${name} {
      pkgs = eknPkgs;
      inherit lib easykubenix;
    }
  );

  perExample = lib.mapAttrs (_: e: e.check) examples;
in
{
  inherit examples;

  checks = perExample // {
    # One derivation that fails if any example fails, so CI has a single thing
    # to build and a new example is covered the moment it joins `names` above.
    # `nix flake check` used to fill this role and needed a flake to do it.
    all = pkgs.runCommand "easykubenix-examples" {
      checks = lib.attrValues perExample;
    } "printf '%s\\n' $checks > $out";
  };

  packages = {
    inherit (examples.basic) manifestJSON manifestYAMLFile;
    # Not part of `checks`: this one boots a real etcd and kube-apiserver, so
    # it is run (`nix run --file ./checks.nix validationScript`) rather than
    # built.
    inherit (examples.validation) validationScript;
  };
}
