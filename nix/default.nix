{
  inputs ? (import ./compat.nix).inputs,
  system ? builtins.currentSystem,
  pkgs ? inputs.nixpkgs.legacyPackages.${system},
}:
rec {
  root = import ../default.nix { inherit pkgs; };
  helmrpc = pkgs.callPackage ../helmrpc { };
  helmrpc-proto = pkgs.python3Packages.callPackage ../helmrpc-proto { protoc = pkgs.protobuf; };
  ekn = pkgs.python3Packages.callPackage ../ekn {
    inherit (root.passthru) nanopynix nanopynix-helpers clypi grpclib-transports;
    inherit helmrpc helmrpc-proto;
  };
  shell = pkgs.python3Packages.callPackage ./shell.nix { inherit ekn helmrpc; };
}
