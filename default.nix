# default.nix
{
  pkgs ? import <nixpkgs> { },
  modules ? [ ./demo ],
  specialArgs ? { },
  debug ? true,
}:
let
  inherit (pkgs) lib;
  attrIf = condition: content: if condition then content else { };

  kubenix = builtins.fetchTree {
    type = "github";
    owner = "hall";
    repo = "kubenix";
    ref = "e8577e661f3286624f79ce5ca3240a0ffcea9a7d";
  };

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
      helm = import "${kubenix}/lib/helm" { inherit pkgs; };
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
