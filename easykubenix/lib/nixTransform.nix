# Run a Nix transform at build time, and read its result back as a value.
#
#   ekn.lib.nixTransform {
#     name = "cilium-dashboard";
#     src = ./dashboard.json;
#     args = { inherit absentMetrics; };
#     transformer = # nix
#       ''
#         { lib, value, args }:
#         value // {
#           panels = builtins.filter (p: !(builtins.elem p.metric args.absentMetrics))
#             (value.panels or [ ]);
#         }
#       '';
#   }
#
# `transformer` also takes a list, applied left to right in the one
# evaluation. A transform that declares no `args` is called without it.
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
  #
  # **The whole directory, on purpose.** Every file here is an input, so an
  # edit to any one of them rebuilds every transform. That is the right side
  # to pay on. This directory changes when easykubenix does, which is a
  # deliberate version bump; a transform is read on every evaluation of every
  # configuration. Narrowing the set buys a rare rebuild and costs a fileset
  # that has to track what the overlay imports, and that silently drops a
  # file a transform needs when it falls behind.
  eknLib = ./.;

  # Fixed, so every transform shares one file and one store path.
  #
  # `<nixpkgs/lib>` rather than `(import <nixpkgs> { }).lib`: the same lib,
  # without evaluating a package set the transform cannot use anyway.
  runner = builtins.toFile "ekn-run-transform.nix" ''
    let
      lib = (import <nixpkgs/lib>).extend (import (builtins.getEnv "EKN_LIB"));
      value = builtins.fromJSON (builtins.readFile (builtins.getEnv "EKN_IN"));
      args = builtins.fromJSON (builtins.readFile (builtins.getEnv "EKN_ARGS"));
      transform = import (builtins.getEnv "EKN_TRANSFORM");
      resolved = lib.walkWithPath lib.kubeAttrsToLists (transform { inherit lib value args; });
    in
    if lib.hasMarker resolved then
      throw '''
        nixTransform: the result still holds a marker after conversion.

        `mkIfExists`, `mkReplaceList` and `mkReplaceWhere` need an option to
        merge against and there is none here. Use plain values, or
        `mkNamedList`/`mkNumberedList`, which this runner converts back to
        lists.
      '''
    else
      resolved
  '';

  # One file holding every transform, folded left to right.
  #
  # `functionArgs` decides whether each one gets `args`: a transform written
  # before `args` existed declares `{ lib, value }`, and calling it with a
  # third attribute is an error rather than something it can ignore.
  compose =
    name: transformer:
    let
      sources = if builtins.isList transformer then transformer else [ transformer ];
    in
    builtins.toFile "${name}-transform.nix" ''
      { lib, value, args }:
      let
        apply =
          f: acc:
          if builtins.functionArgs f ? args then
            f { inherit lib args; value = acc; }
          else
            f { inherit lib; value = acc; };
      in
      builtins.foldl' (acc: f: apply f acc) value [
      ${lib.concatMapStringsSep "\n" (source: "(\n${source}\n)") sources}
      ]
    '';

  transform =
    {
      # Names the derivation and the generated transform file.
      #
      # **Every error from inside the transform names this file and nothing
      # else**, so it is the only thing that says which value failed. Nix
      # reports the line and the caret and, for a misspelled attribute, a
      # suggestion -- all against `<name>-transform.nix` in the store:
      #
      #   error: attribute 'absentMetricz' missing
      #   at /nix/store/...-cilium-dashboard-transform.nix:26:50:
      #   Did you mean absentMetrics?
      #
      # So name it after the value, not after the caller: a prefix the value
      # already carries reads as `cilium-cilium-dashboard`.
      name,
      # The input, as JSON. For a value already in Nix, use
      # `pkgs.writeText "x.json" (builtins.toJSON value)`.
      #
      # **Not `builtins.toFile`.** It refuses a string that carries store
      # context, so a value naming a chart path or an image fails with
      # "files created by builtins.toFile may not reference derivations".
      # `writeText` takes context and is otherwise the same.
      src,
      # Nix source for a function `{ lib, value, args }: ...`, or a list of
      # them applied left to right. A function that declares no `args` is
      # called without it, so `{ lib, value }: ...` still works.
      #
      # A list runs inside the one sandboxed evaluation, so two transforms
      # over one value cost one derivation and one JSON round trip. The
      # marker check runs once, at the end.
      transformer,
      # Data the transform needs that is neither `lib` nor `value` -- a
      # configuration it is derived from, typically. It travels as JSON and
      # never becomes Nix source.
      #
      # **This is why `args` exists rather than "interpolate it into the
      # transformer".** The obvious way to do that is wrong: `builtins.toJSON
      # ["a" "b"]` gives `["a","b"]`, and a Nix list has no commas, so the
      # generated file is a syntax error nothing sees until build time.
      args ? { },
    }:
    builtins.fromJSON (
      builtins.readFile (
        pkgs.runCommand "${name}-transformed.json"
          {
            EKN_IN = src;
            EKN_TRANSFORM = compose name transformer;
            # `writeText` and not `toFile`, for the reason `src` gives: a
            # configuration can name a store path.
            EKN_ARGS = pkgs.writeText "${name}-args.json" (builtins.toJSON args);
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
