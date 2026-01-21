# default.nix
{
  pkgs ? import <nixpkgs> { },
  modules ? [ ./demo ],
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
  deploymentScript = eval.config.kluctl.script;
  validationScript = eval.config.validation.script;
}
# Add debug attributes if debug is set
// (attrIf debug {
  inherit pkgs lib eval;
})
