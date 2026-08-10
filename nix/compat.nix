let
  flake-compatish = import (
    fetchTree (builtins.fromJSON (builtins.readFile ../flake.lock)).nodes.flake-compatish.locked
  );

  # `<nixpkgs>` throws outright when NIX_PATH carries no `nixpkgs` entry, which
  # is the normal state of a CI runner. It cannot be left unresolved in the
  # attrset below: flake-compatish forces an override value with `!= null`
  # (`resolveNamedOverride`) *before* the `tryEval` guard in `resolveOverride`
  # ever runs, so a throwing value escapes that guard and fails the whole
  # evaluation rather than falling back.
  #
  # This only started mattering when CI moved off flake commands: `nix build
  # --file`, `nix run --file` and `nix-shell` all read this file, and
  # `nix flake check` never did. Resolve it here instead, and simply omit the
  # override when there is no channel to point at -- the lockfile is the
  # fallback, and pinned inputs are what CI should be building from anyway.
  channelNixpkgs = builtins.tryEval <nixpkgs>;
in
flake-compatish {
  source = ../.;
  # A missing path is not an error: `resolveOverride` checks `pathExists` and
  # falls back to the lockfile, so these local development checkouts simply do
  # not apply on a machine that lacks them.
  overrides = {
    adios = ../../adios;
    self = ../.;
    nanopynix = /home/lillecarl/Code/nanopynix;
  }
  // (if channelNixpkgs.success then { nixpkgs = channelNixpkgs.value; } else { });
}
