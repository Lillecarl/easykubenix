{
  inputs ? (import ./nix/compat.nix).inputs,
  system ? builtins.currentSystem,
  pkgs ? inputs.nixpkgs.legacyPackages.${system},
  modules ? [ ./demo ],
  specialArgs ? { },
  debug ? null, # unused but kept for API compatibility
}:
let
  pkgs' = pkgs.extend (import ./easykubenix/pkgs/default.nix);
in
let
  pkgs = pkgs';
  lib = pkgs.lib;

  nanopynix = import inputs.nanopynix { inherit pkgs; };
  adios = (import inputs.adios).adios;
  template = definition: adios definition { };
  clypi = pkgs.python3Packages.callPackage ./nix/clypi.nix { };
  ekn = pkgs.python3Packages.callPackage ./ekn {
    inherit (nanopynix) nanopynix nanopynix-helpers;
    inherit clypi;
  };
  easykubenix-docs = pkgs.python3Packages.callPackage ./nix/docs.nix { };

  eval = lib.evalModules {
    specialArgs = {
      inherit adios template;
    }
    // specialArgs;

    modules = [
      {
        _module.args = {
          inherit pkgs;
          inherit (pkgs) lib;
          # Helper functions for consuming modules, distinct from `object.ekn`
          # (GitOps-routing metadata on rendered Kubernetes objects, see
          # kubernetes.nix's `ekn.argo`/`ekn.flux`/`eknByPath`) and the
          # top-level `ekn` CLI package bound above (exposed via
          # `passthru.ekn`, never visible to modules) -- same word, three
          # unrelated meanings in this codebase, kept in separate namespaces
          # by construction (module function-arg vs. object attrset field
          # vs. outer let-binding), but worth this note so nobody conflates
          # them.
          ekn = {
            # Recursively wrap every leaf of an attrset in `lib.mkDefault`,
            # so a module's baked-in option value (e.g. a Helm chart's
            # `values`) stays overridable leaf-by-leaf instead of a caller's
            # single definition replacing the whole thing.
            mkDefaults = lib.mapAttrsRecursive (_: v: lib.mkDefault v);
          };
        };
      }
      ./easykubenix/assertions.nix
      ./easykubenix/gitops.nix
      ./easykubenix/helm.nix
      ./easykubenix/importyaml.nix
      ./easykubenix/internal.nix
      ./easykubenix/kluctl.nix
      ./easykubenix/kubernetes.nix
      ./easykubenix/lib.nix
      ./easykubenix/validation.nix
    ]
    ++ modules;
  };
in
{
  inherit (eval.config.internal)
    manifestAttrs
    manifestJSON
    manifestJSONFile
    manifestYAML
    manifestYAMLFile
    manifestYAMLList
    manifestYAMLFileList
    ;

  inherit (eval) config;

  deploymentScript = eval.config.kluctl.script;
  validationScript = eval.config.validation.script;
  passthru = {
    inherit (nanopynix) nanopynix nanopynix-bindings nanopynix-helpers grpclib-transports;
    inherit
      inputs
      pkgs
      lib
      adios
      eval
      ekn
      clypi
      easykubenix-docs
      ;
  };
}
