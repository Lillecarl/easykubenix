{
  inputs ?
    (
      let
        flake-compatish = import (
          fetchTree (builtins.fromJSON (builtins.readFile ./flake.lock)).nodes.flake-compatish.locked
        );
      in
      flake-compatish {
        source = ./.;
        # Use nixpkgs from NIX_PATH if configured
        overrides =
          let
            nixpkgs = builtins.tryEval <nixpkgs>;
            nanopynix = builtins.tryEval /home/lillecarl/Code/nanopynix;
          in
          (
            if nixpkgs.success then
              builtins.warn "using nixpkgs from NIX_PATH" {
                nixpkgs = nixpkgs.value;
              }
            else
              builtins.warn "using nixpkgs from flake.lock" { }
          )
          // (
            if nanopynix.success then
              builtins.warn "using nanopynix from NIX_PATH" {
                nanopynix = nanopynix.value;
              }
            else
              builtins.warn "using nanopynix from flake.lock" { }
          );
      }
    ).inputs,
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
  ekn = pkgs.python3Packages.callPackage ./ekn { inherit (nanopynix) nanopynix; };

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

  inherit (nanopynix) nanopynix nanopynix-bindings;

  inherit ekn;

  deploymentScript = eval.config.kluctl.script;
  validationScript = eval.config.validation.script;
  debug = {
    inherit
      inputs
      pkgs
      lib
      eval
      ekn
      ;
  };
}
