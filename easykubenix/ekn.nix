{
  config,
  lib,
  ...
}:
{
  options.ekn = {
    cacheTo = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = ''
        Destination Nix store URI (e.g. "ssh-ng://user@host:2222") that `ekn
        deploy` automatically pushes `ekn.cachePackage`'s closure to, before
        committing/pushing GitOps manifests to the remote. `null` disables
        the cache push entirely.
      '';
      example = "ssh-ng://nix@cache.example.com:2222";
    };

    cachePackage = lib.mkOption {
      type = lib.types.package;
      default = config.internal.manifestJSONFile;
      description = ''
        Derivation whose full closure gets pushed to `ekn.cacheTo`. Defaults
        to `internal.manifestJSONFile` -- the same manifest-JSON derivation
        `ekn validate` already builds, rather than a fresh whole-cluster
        dump. Nix string context means its closure automatically includes
        every store path referenced anywhere in the generated manifests
        (e.g. CSI-mounted store paths embedded in `volumeAttributes`),
        without needing to enumerate them by hand. Override only if a
        project needs a different (narrower or wider) closure pushed
        instead.
      '';
    };
  };
}
