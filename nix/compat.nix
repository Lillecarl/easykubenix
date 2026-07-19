let
  flake-compatish = import (
    fetchTree (builtins.fromJSON (builtins.readFile ../flake.lock)).nodes.flake-compatish.locked
  );
in
flake-compatish {
  source = ../.;
  overrides = {
    adios = ../../adios;
    self = ../.;
    nixpkgs = <nixpkgs>;
    nanopynix = /tmp/nanopynix-1;
  };
}
