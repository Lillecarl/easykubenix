# Run a Nix transform at build time, and read its result back as a value.
#
#   ekn.lib.nixTransform {
#     name = "cilium-dashboard";
#     src = ./dashboard.json;
#     transformer = # nix
#       ''
#         { lib, value }:
#         value // { panelCount = builtins.length (value.panels or [ ]); }
#       '';
#   }
#
# The transform is Nix source, not a Nix function: it is written to a file
# and a sandboxed evaluator imports it. So a value can be carried whole
# through `mkUntyped` -- which nothing walks, and therefore nothing can
# transform -- and still be transformed, once, in a derivation the store
# caches. The evaluator that renders the cluster only reads the JSON.
#
# The sandboxed evaluator runs against `dummy://`. It can read files and
# evaluate. It cannot realise a derivation. So a transform, and anything
# shipped to it, must be plain value manipulation.
{
  lib,
  pkgs,
  # The nixpkgs the sandboxed evaluator reads, as `nix/fetch.nix` returns it.
  #
  # **A string naming a store path, not a path value.** `fetch.nix` returns a
  # string for a locked source and a path value for a working-copy override.
  # Interpolating a path value *imports* it: measured 6.3s and a second copy
  # of nixpkgs in the store, 334 MB under a new hash, on every evaluation.
  # The string carries store context, so it creates the dependency and copies
  # nothing -- measured 0.36s. See issue #33.
  nixpkgs,
}:
let
  # This directory: easykubenix's `lib.extend` overlay, shipped into the
  # sandbox so a transform can call `mkNamedList` and the rest. Every file
  # here is value-only except `parseYamlStream.nix`, `serialiseYaml.nix`,
  # `tofuRegistry.nix` and `importHelm.nix`, which take `pkgs` -- the overlay
  # in `default.nix` imports none of those.
  eknLib = ./.;

  # Fixed, so every transform shares one file and one store path.
  #
  # `<nixpkgs/lib>` rather than `(import <nixpkgs> { }).lib`: the same lib,
  # without evaluating a package set the transform cannot use anyway.
  runner = builtins.toFile "ekn-run-transform.nix" ''
    let
      lib = (import <nixpkgs/lib>).extend (import (builtins.getEnv "EKN_LIB"));
      value = builtins.fromJSON (builtins.readFile (builtins.getEnv "EKN_IN"));
      transform = import (builtins.getEnv "EKN_TRANSFORM");
      resolved = lib.walkWithPath lib.kubeAttrsToLists (transform { inherit lib value; });
    in
    if lib.hasMarker resolved then
      throw '''
        nixTransform: the result still holds a marker after conversion.

        `mkIfExists` needs an option to merge against and there is none
        here. Use plain values, or `mkNamedList`/`mkNumberedList`, which
        this runner converts back to lists.
      '''
    else
      resolved
  '';

  transform =
    {
      # Names the derivation and the generated transform file. A throw inside
      # the transform reports that file and nothing else, so name this after
      # the option path that produced it.
      name,
      # The input, as JSON. For a value already in Nix:
      # `builtins.toFile "x.json" (builtins.toJSON value)`.
      src,
      # Nix source for a function `{ lib, value }: ...`.
      transformer,
    }:
    builtins.fromJSON (
      builtins.readFile (
        pkgs.runCommand "${name}-transformed.json"
          {
            EKN_IN = src;
            EKN_TRANSFORM = builtins.toFile "${name}-transform.nix" transformer;
            EKN_LIB = eknLib;
            NIX_PATH = "nixpkgs=${nixpkgs}";
            # `pkgs.nix` costs about 0.6s to force, once per evaluation --
            # not once per derivation. Measured: 1 derivation 0.851s, 10
            # 0.943s, 100 1.116s, against 0.245s for a bare `runCommand`.
            # Nothing forces it unless a configuration uses a transform.
            nativeBuildInputs = [ pkgs.nix ];
          }
          # bash
          ''
            export HOME=$TMPDIR
            nix --extra-experimental-features nix-command \
              eval --store dummy:// --impure --json --file ${runner} > $out
          ''
      )
    );
in
lib.warnIf (builtins.isPath nixpkgs) ''
  nixTransform: `nixpkgs` is a path value, so every evaluation re-imports
  it -- measured 6.3s for nixpkgs. This happens when a working copy
  overrides the source, which has no store path to name yet. See #33.
'' transform
