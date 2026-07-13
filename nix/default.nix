{
  inputs ? (import ./compat.nix).inputs,
  pkgs ? import inputs.nixpkgs { },
}:
rec {
  root = import ../default.nix { inherit pkgs; };
  ekn = pkgs.python3Packages.callPackage ../ekn { inherit (root.passthru) nanopynix clypi; };
  shell = pkgs.python3Packages.callPackage ./shell.nix { inherit ekn; };
}
