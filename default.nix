{
  inputs ? (import ./nix/compat.nix).inputs,
  pkgs ? import inputs.nixpkgs { },
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
  clypi = pkgs.python3Packages.callPackage ./nix/clypi.nix { };
  ekn = pkgs.python3Packages.callPackage ./ekn {
    inherit (nanopynix) nanopynix;
    inherit clypi;
  };

  eval = lib.evalModules {
    inherit specialArgs;

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
    inherit (nanopynix) nanopynix nanopynix-bindings;
    inherit
      inputs
      pkgs
      lib
      eval
      ekn
      clypi
      ;
  };
}
