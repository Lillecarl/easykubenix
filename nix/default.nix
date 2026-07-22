{
  inputs ? (import ./compat.nix).inputs,
  system ? builtins.currentSystem,
  pkgs ? inputs.nixpkgs.legacyPackages.${system},
}:
rec {
  root = import ../default.nix { inherit pkgs; };
  ekn = pkgs.python3Packages.callPackage ../ekn {
    inherit (root.passthru) nanopynix nanopynix-helpers clypi kr8s;
  };
  shell = pkgs.python3Packages.callPackage ./shell.nix { inherit ekn; };
}
