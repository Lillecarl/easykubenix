# default.nix
{
  pkgs ? import <nixpkgs> { },
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
      ./internal.nix
      ./kubernetes.nix
      ./helm.nix
      ./kluctl.nix
      ./validation.nix
      ./importyaml.nix
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

  deploymentScript = eval.config.kluctl.script;
  validationScript = eval.config.validation.script;
}
