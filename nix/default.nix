{
  inputs ? (import ./compat.nix).inputs,
  system ? builtins.currentSystem,
  pkgs ? inputs.nixpkgs.legacyPackages.${system},
}:
rec {
  root = import ../default.nix { inherit pkgs; };
  shell = pkgs.python3Packages.callPackage ./shell.nix { };
}
