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
