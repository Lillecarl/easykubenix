# Where every dependency of this repository lives.
#
# nixidae is the umbrella that holds it, and the umbrella owns every source.
# Inside the umbrella that is the checkout two directories up. Outside it,
# the umbrella is fetched, and this working copy is put in place of the copy
# that came down with it. Either way the answer is the same one, so a build
# here and a build from the umbrella agree.
#
# A plain tarball is enough. The umbrella records every revision in
# nix/sources.lock, a file in its own tree, and the working copies beside it
# are ignored rather than committed, so a fetch that brings down none of them
# still resolves all of them.
#
# This repository is not a flake. Every entry point here takes the set this
# file returns and imports what it wants.
let
  # Where this checkout sits. Inside the umbrella, whether that umbrella is a
  # working copy or a pinned store path, this is `<umbrella>/easykubenix`.
  # Fetched on its own, it is a store path with nothing above it.
  root = toString ../.;

  # `../..` from a bare store path leaves the store root, and Nix refuses to
  # evaluate that at all: "'nix' is too short to be a valid store path".
  # `builtins.tryEval` does not catch it -- measured, not assumed -- so the
  # question has to be avoided rather than caught.
  #
  # A bare store path is exactly `/nix/store/` plus one component. Anything
  # deeper has a directory between this checkout and the store root, which is
  # precisely the case where the umbrella is the thing in the store and this
  # checkout is inside it. That case is safe to ask about, and it is the
  # normal case for anyone who pins the umbrella.
  escapesStore = builtins.match "/nix/store/[^/]+" root != null;

  # Being in the store does not mean the umbrella is absent. An earlier
  # version tested "not in the store", which is only ever true in a working
  # copy -- so every downstream project that pinned the umbrella silently
  # took the fetch below instead of the umbrella it shipped inside, and got
  # whatever revision the eval cache happened to hold. That is the version
  # skew the umbrella exists to make impossible.
  inUmbrella = !escapesStore && builtins.pathExists ../../nix/wire.nix;

  # The umbrella itself, when this checkout is on its own.
  #
  # **This fetch is the one the umbrella cannot cover.** Every other source
  # goes through the umbrella's own `nix/resolve.nix`, which UMBRELLA_GIT
  # already reaches. This one has to find the umbrella first, so it reads the
  # variable a second time.
  #
  # `github:` here is unlocked -- no revision, no narHash -- so Nix asks
  # api.github.com for the head of the default branch on every evaluation
  # that `tarball-ttl` does not answer from cache. That is one call per job
  # before any source is resolved at all. Anonymous api.github.com allows 60
  # an hour per IP and GitHub's runners share a NAT pool.
  #
  # The git reference resolves the same head over the git protocol, which
  # that limit does not count.
  umbrellaRef =
    if builtins.getEnv "UMBRELLA_GIT" != "" then
      "git+https://github.com/nixidae/nixidae?shallow=1"
    else
      "github:nixidae/nixidae";

  wire =
    if inUmbrella then
      ../../nix/wire.nix
    else
      (builtins.fetchTree (builtins.parseFlakeRef umbrellaRef)).outPath + "/nix/wire.nix";
in
import wire { overrides.easykubenix = ../.; }
