{
  inputs = {
    adios.url = "github:adisbladis/adios";
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
          inherit (defNix.passthru)
            nanopynix
            nanopynix-bindings
            nanopynix-helpers
            easykubenix-docs
            # This repository's own build of the CLI, from `ekn/`. See
            # `eknCli` in default.nix.
            ekn
            ;
        }
      );
      # Re-exported, not defined here. These are plain derivations built by
      # `nix build --file ./checks.nix all`, so the gates do not need a flake
      # command to run -- see nix/default.nix and docs/examples/default.nix.
      checks = forEachSystem (
        system:
        (import ./nix {
          inherit inputs system;
          pkgs = inputs.self.legacyPackages.${system};
        }).checks
      );

      lib.easykubenix = import ./default.nix;

      devShells = forEachSystem (
        system:
        let
          pkgs = inputs.nixpkgs.legacyPackages.${system};
          # This repository's own venv, holding `ekn`'s dependency closure.
          # See `eknDevEnv` in ./default.nix and nix/shell.nix.
          easykubenix = import ./default.nix { inherit pkgs; };
        in
        {
          default = pkgs.python3Packages.callPackage ./nix/shell.nix {
            ruff = pkgs.ruff;
            inherit (easykubenix.passthru) eknDevEnv;
          };
        }
      );
      legacyPackages = forEachSystem (system: inputs.nixpkgs.legacyPackages.${system});
    };
}
