{
  pkgs ? import <nixpkgs> { },
}:
rec {
  root = import ../default.nix { inherit pkgs; };
  ekn = pkgs.python3Packages.callPackage ../ekn { inherit (root) nanopynix; };
  shell = pkgs.python3Packages.callPackage ./shell.nix { inherit ekn; };
}
