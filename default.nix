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
          in
          if nixpkgs.success then
            builtins.warn "using nixpkgs from NIX_PATH" {
              nixpkgs = nixpkgs.value;
              nanopynix = /home/lillecarl/Code/nanopynix;
            }
          else
            builtins.warn "using nixpkgs from flake.lock" { };
      }
    ).inputs,
  pkgs ? import inputs.nixpkgs { },
  modules ? [ ./demo ],
  specialArgs ? { },
  debug ? null, # unused but kept for API compatibility
}:
let
  pkgs' = pkgs.extend (import ./pkgs/default.nix);
in
let
  pkgs = pkgs';
  lib = pkgs.lib;

  nanopynix = import inputs.nanopynix { inherit pkgs; };
  ekn = pkgs.python3Packages.callPackage ./pkgs/ekn { inherit (nanopynix) nanopynix; };

  eval = lib.evalModules {
    inherit specialArgs;

    modules = [
      {
        _module.args = {
          inherit pkgs;
          inherit (pkgs) lib;
        };
      }
      ./assertions.nix
      ./helm.nix
      ./importyaml.nix
      ./internal.nix
      ./kluctl.nix
      ./kubernetes.nix
      ./lib.nix
      ./validation.nix
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
  inherit
    pkgs
    lib
    eval
    ;

  inherit ekn;
  inherit (nanopynix) nanopynix nanopynix-bindings;

  deploymentScript = eval.config.kluctl.script;
  validationScript = eval.config.validation.script;
}
