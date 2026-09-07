# The doc examples, and the checks over them.
#
# This is where the examples are wired up, and it is now the only place. A
# `flake.nix` beside it used to call this file and re-expose the result; this
# repository is not a flake any more, and nix/sources.nix says where its
# dependencies come from instead.
#
# Build them with `nix build --file ./checks.nix all` from the repository root.
{
  sources ? import ../../nix/sources.nix,
  system ? builtins.currentSystem,
  pkgs ? import sources.nixpkgs {
    inherit system;
    config.allowUnfree = true;
  },
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
    "bootstrap"
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
    # Not part of `checks`: these boot a real etcd and kube-apiserver, so they
    # are run (`nix run --file ./nix packages.validationScript` from the
    # repository root) rather than built. ../../checks.nix exposes only
    # `checks`, so it cannot reach them.
    inherit (examples.validation) validationScript;
    # The bootstrap target's own instance, applied through the same harness --
    # ArgoCD's CRDs and the Application that needs them. Named separately
    # because `kubernetes.generated` deliberately excludes bootstrap objects,
    # so `validationScript` above can never cover them.
    bootstrapValidationScript = examples.bootstrap.validationScript;
  };
}
