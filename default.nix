# default.nix
{
  pkgs ? import <nixpkgs> { },
  modules ? [ ./demo ],
  specialArgs ? { },
  debug ? true,
}:
let
  pkgs' = pkgs.extend (import ./pkgs/default.nix);
in
let
  pkgs = pkgs';
  lib = pkgs.lib;
  attrIf = condition: content: if condition then content else { };

  eval = lib.evalModules {
    modules = [
      ./internal.nix
      ./kubernetes.nix
      ./helm.nix
      ./kluctl.nix
      ./validation.nix
      ./importyaml.nix
    ]
    ++ modules;
    specialArgs = {
      inherit pkgs;
      inherit (pkgs) lib;
    }
    // specialArgs;
  };
in
{
  inherit (eval.config.internal)
    manifestAttrs
    manifestJSON
    manifestJSONFile
    manifestYAML
    manifestYAMLFile
    ;
  deploymentScript = eval.config.kluctl.script;
  validationScript = eval.config.validation.script;
}
# Add debug attributes if debug is set
// (attrIf debug {
  inherit pkgs lib eval;
})
