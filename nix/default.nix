{
  pkgs ? import <nixpkgs> { },
}:
rec {
  shell = pkgs.python3Packages.callPackage ./shell.nix { };
}
