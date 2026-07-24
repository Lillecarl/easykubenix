{
  inputs ? (import ./compat.nix).inputs,
  system ? builtins.currentSystem,
  pkgs ? inputs.nixpkgs.legacyPackages.${system},
}:
let
  # `ekn`'s source lives in ../ekn, but its dependency closure comes from
  # nanopynix' exported development environment. See shell.nix.
  nanopynix = import inputs.nanopynix { inherit pkgs; };

  root = import ../default.nix { inherit pkgs; };

  # The gates over the doc examples. Plain derivations, so they build from a
  # bare `import` and do not need `nix flake check` -- see ../checks.nix for
  # the entry point CI uses, and ../docs/examples/default.nix for what they
  # are. `flake.nix` re-exports these; it is not where they live.
  examples = import ../docs/examples { inherit inputs system pkgs; };

  # `ekn` runs inside a Nix build sandbox from three derivations in this
  # repository -- `_yamlToJson` (lib/parseYamlStream.nix, pkgs/renderChart.nix),
  # `_jsonToYAML` and `split-manifest` (both easykubenix/internal.nix) -- and a
  # sandbox has no ambient trust store. `ekn` imports pygit2, which initialises
  # OpenSSL at import and refuses to start without one, so until the CA bundle
  # moved onto the program itself it could not run here at all (nanopynix issue
  # #62, fixed by `mkApp`'s `caBundle` wrapper). This derivation is that
  # sandbox, so the crash cannot come back unseen.
  #
  # The gate belongs here and not in nanopynix, even though the wrapper it
  # guards is nanopynix': every call site that runs `ekn` without certificates
  # is in this repository. A test under ../tests could not stand in for it,
  # because the `ekn` on the dev shell's PATH is the venv's own console script,
  # not the wrapped application.
  #
  # The YAML 1.1 case is deliberate. `0644` reads as octal 420 there, which is
  # the scalar semantics this out-of-process fallback exists to keep.
  ekn-sandbox =
    pkgs.runCommand "easykubenix-check-ekn-sandbox"
      {
        nativeBuildInputs = [ root.passthru.ekn ];
      }
      ''
        printf 'a: 1\n---\nb: 0644\n' | ekn _yamlToJson --yaml-version yaml11 >got.json
        printf '%s' '[{"a":1},{"b":420}]' >want.json
        diff -u want.json got.json

        printf '%s' '{"a":1,"b":"x"}' | ekn _jsonToYAML >got.yaml
        printf 'a: 1\nb: x\n' >want.yaml
        diff -u want.yaml got.yaml

        touch "$out"
      '';
in
{
  inherit root ekn-sandbox;
  inherit (examples) packages;

  shell = pkgs.python3Packages.callPackage ./shell.nix {
    inherit (root.passthru) eknDevEnv;
  };

  checks = examples.checks // {
    inherit ekn-sandbox;
    # `all` is what CI builds, so a gate that is not in it is a gate that does
    # not run. ../docs/examples/default.nix builds its own `all` over the
    # examples; this one is that plus everything added here.
    all = pkgs.runCommand "easykubenix-checks" {
      checks = [
        examples.checks.all
        ekn-sandbox
      ];
    } "printf '%s\\n' $checks > $out";
  };
}
