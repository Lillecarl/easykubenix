{
  description = "easykubenix examples — evaluatable in CI";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixpkgs-unstable";
    easykubenix = {
      url = "path:../../";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  # A wrapper over ./default.nix, which is where the examples are wired up.
  # Nothing here may grow logic of its own: the non-flake path is the one that
  # has to work, and a second definition would let the two disagree.
  outputs =
    { easykubenix, nixpkgs, ... }:
    let
      forEachSystem = nixpkgs.lib.genAttrs [
        "x86_64-linux"
        "aarch64-linux"
      ];
      examplesFor =
        system:
        import ./. {
          pkgs = import nixpkgs { inherit system; };
          easykubenix = easykubenix.lib.easykubenix;
        };
    in
    {
      checks = forEachSystem (system: (examplesFor system).checks);
      packages = forEachSystem (system: (examplesFor system).packages);
    };
}
