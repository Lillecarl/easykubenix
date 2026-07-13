let
  flake-compatish = import (
    fetchTree (builtins.fromJSON (builtins.readFile ../flake.lock)).nodes.flake-compatish.locked
  );
in
flake-compatish {
  source = ../.;
  overrides = {
    nixpkgs = <nixpkgs>;
    nanopynix = ../../nanopynix;
  };
}
