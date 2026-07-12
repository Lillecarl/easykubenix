{
  description = "easykubenix examples — evaluatable in CI";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixpkgs-unstable";
    easykubenix = {
      url = "path:../../";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    { easykubenix, nixpkgs, ... }:
    let
      eknLib = easykubenix.lib.easykubenix;
      forEachSystem = nixpkgs.lib.genAttrs [ "x86_64-linux" "aarch64-linux" ];

      eknPkgsFor = system: (eknLib {
        pkgs = import nixpkgs { inherit system; };
        modules = [ ];
      }).pkgs;

      example = system: name: import ./${name} {
        pkgs = eknPkgsFor system;
        inherit (nixpkgs) lib;
        easykubenix = eknLib;
      };

      checksFor = system: {
        basic = (example system "basic").check;
        namedlists = (example system "namedlists").check;
        helm = (example system "helm").check;
        generators = (example system "generators").check;
        edge-cases = (example system "edge-cases").check;
        validation = (example system "validation").check;
      };

    in
    {
      checks = forEachSystem checksFor;
      packages = forEachSystem (system: {
        inherit (example system "basic") manifestJSON manifestYAMLFile;
        validationScript = (example system "validation").validationScript;
      });
    };
}
