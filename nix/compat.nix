let
  flake-compatish = import (
    fetchTree (builtins.fromJSON (builtins.readFile ../flake.lock)).nodes.flake-compatish.locked
  );
in
flake-compatish {
  source = ../.;
  # Use nixpkgs from NIX_PATH if configured
  overrides =
    let
      nixpkgs = builtins.tryEval <nixpkgs>;
      nanopynix = builtins.tryEval /home/lillecarl/Code/nanopynix;
    in
    (
      if nixpkgs.success then
        builtins.warn "using nixpkgs from NIX_PATH" {
          nixpkgs = nixpkgs.value;
        }
      else
        builtins.warn "using nixpkgs from flake.lock" { }
    )
    // (
      if nanopynix.success then
        builtins.warn "using nanopynix from NIX_PATH" {
          nanopynix = nanopynix.value;
        }
      else
        builtins.warn "using nanopynix from flake.lock" { }
    );
}
