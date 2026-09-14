# The OpenTofu registry as a Nix attrset of provider derivations.
#
# Adapted from hetzkube's `tf/registry.nix` (lillecarl), which is where this
# approach was worked out.
#
# nixpkgs' `terraform-providers` is a curated subset: 169 providers, one
# version each, built from Go source against hashes a human updates. That is
# enough to pin *what nixpkgs packages* and nothing else. This reads the
# registry's own index instead -- `providers/<shard>/<owner>/<repo>.json`,
# 4242 of them -- where every version of every provider names a prebuilt
# download and its shasum.
#
# So the pin moves from "whatever nixpkgs chose" to "the registry revision the
# umbrella locked, plus a per-file shasum". `umbrella update opentofu-registry`
# moves it, and the lock records it like any other source.
#
# Laziness is what makes the size bearable. The tree is ~424M and the fold
# below walks every file, but `importJSON` is only forced for a provider
# somebody actually reads: reaching one provider's latest version costs about
# a second, and a configuration that names none costs nothing at all.
{
  pkgs,
  lib,
  registry,
}:
let
  # A provider as the registry ships it: a prebuilt zip, not a Go build.
  #
  # The install path is the one `-plugin-dir` expects, and it is load-bearing
  # rather than tidy -- OpenTofu finds a provider by walking exactly this
  # shape. `registry.opentofu.org` and not `registry.terraform.io`, matching
  # what nixpkgs' `withPlugins` rewrites its own tree to, so the two kinds of
  # provider can sit in one wrapper.
  mkProvider = lib.makeOverridable (
    {
      owner,
      repo,
      version,
      url,
      sha256,
      registryHost ? "registry.opentofu.org",
    }:
    let
      inherit (pkgs.stdenv.hostPlatform.go) GOARCH GOOS;
      installPath = "$out/libexec/terraform-providers/${registryHost}/${owner}/${repo}/${version}/${GOOS}_${GOARCH}";
    in
    pkgs.stdenvNoCC.mkDerivation {
      pname = "terraform-provider-${owner}-${repo}";
      inherit version;

      src = pkgs.fetchurl { inherit url sha256; };

      dontUnpack = true;
      nativeBuildInputs = [ pkgs.unzip ];

      installPhase = ''
        runHook preInstall
        mkdir -p "${installPath}"
        unzip -o $src -d "${installPath}"
        chmod +x "${installPath}"/terraform-provider-*
        runHook postInstall
      '';

      passthru = {
        # What `tofu.terraform.required_providers` needs, carried on the
        # derivation that provides the binary. Declaring the two separately is
        # how a configuration ends up asking for a version it did not pin.
        providerName = repo;
        providerConfig = {
          source = "${registryHost}/${owner}/${repo}";
          inherit version;
        };
      };

      meta = {
        description = "OpenTofu provider ${owner}/${repo}";
        platforms = lib.platforms.unix;
      };
    }
  );

  # One provider's file: every version that ships a build for this platform,
  # keyed by version, plus `latest` and two selectors.
  readProvider =
    {
      owner,
      repo,
      file,
    }:
    let
      inherit (pkgs.stdenv.hostPlatform.go) GOARCH GOOS;
      data = builtins.fromJSON (builtins.readFile file);

      versions = lib.listToAttrs (
        lib.concatMap (
          entry:
          let
            target = lib.findFirst (t: t.os == GOOS && t.arch == GOARCH) null entry.targets;
          in
          lib.optional (target != null) {
            name = entry.version;
            value = mkProvider {
              inherit owner repo;
              inherit (entry) version;
              url = target.download_url;
              sha256 = target.shasum;
            };
          }
        ) data.versions
      );

      # `builtins.sort` with `versionOlder` rather than the file's order: the
      # registry lists newest first today, and depending on that would make
      # `latest` a property of their formatting.
      ordered = builtins.sort lib.versionOlder (lib.attrNames versions);
      newest = predicate: lib.last (lib.filter predicate ordered);
    in
    versions
    // {
      latest = versions.${lib.last ordered};

      # The selector that earns its place. A provider's major versions are
      # rarely compatible, so "newest below 6.0.0" is the real question, and
      # pinning an exact string means editing it to take a patch release.
      latestWhere = predicate: versions.${newest predicate};

      selectVersions = predicate: lib.filterAttrs (version: _: predicate version) versions;
    };

  # `providers/<shard>/<owner>/<repo>.json` -- three levels, the first being a
  # single-character shard directory. Taking the last two components gets
  # owner and repo whether or not that sharding stays.
  files = lib.filesystem.listFilesRecursive (registry + "/providers");
in
lib.foldl' (
  acc: file:
  let
    parts = lib.takeEnd 2 (lib.splitString "/" (builtins.unsafeDiscardStringContext (toString file)));
    owner = lib.head parts;
    repo = lib.removeSuffix ".json" (lib.last parts);
  in
  acc
  // {
    ${owner} = (acc.${owner} or { }) // {
      ${repo} = readProvider { inherit owner repo file; };
    };
  }
) { } files
