let
  flake-compatish = import (
    fetchTree (builtins.fromJSON (builtins.readFile ../flake.lock)).nodes.flake-compatish.locked
  );
in
flake-compatish {
  source = ../.;
  overrides = {
    self = ../.;
    nixpkgs = <nixpkgs>;
    nanopynix = ../../nanopynix;
  };
}
