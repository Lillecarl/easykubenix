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
      eachPkgs = forEachSystem (system: import inputs.nixpkgs { inherit system; });
      eachDefNix = forEachSystem (system: import ./. { pkgs = eachPkgs.${system}; });
    in
    {
      packages = forEachSystem (
        system:
        let
          defNix = eachDefNix.${system};
        in
        {
          inherit (defNix) ekn nanopynix nanopynix-bindings;
        }
      );
      lib.easykubenix = import ./default.nix;
    };
}
