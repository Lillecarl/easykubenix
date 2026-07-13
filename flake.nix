{
  inputs = {
    flake-compatish.url = "github:lillecarl/flake-compatish";
    nixpkgs.url = "github:nixos/nixpkgs/nixpkgs-unstable";
    nanopynix = {
      url = "github:lillecarl/nanopynix/develop";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };
  outputs =
    inputs:
    let
      inherit (inputs.nixpkgs) lib;
      forEachSystem = lib.genAttrs lib.systems.flakeExposed;
      eachDefNix = forEachSystem (
        system:
        import ./. {
          inherit system;
          pkgs = inputs.self.legacyPackages.${system};
        }
      );
    in
    {
      packages = forEachSystem (
        system:
        let
          defNix = eachDefNix.${system};
        in
        {
          inherit (defNix.passthru) ekn nanopynix nanopynix-bindings;
        }
      );
      lib.easykubenix = import ./default.nix;

      devShells = forEachSystem (
        system:
        let
          pkgs = inputs.self.nixpkgs.legacyPackages.${system};
          defNix = eachDefNix.${system};
        in
        {
          default = pkgs.python3Packages.callPackage ./nix/shell.nix {
            ekn = defNix.passthru.ekn;
            ruff = pkgs.ruff;
          };
        }
      );
      legacyPackages = forEachSystem (system: inputs.nixpkgs.legacyPackages.${system});
    };
}
