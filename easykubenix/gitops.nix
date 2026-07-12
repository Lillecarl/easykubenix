{
  lib,
  ...
}:
{
  options.gitops = {
    enable = lib.mkEnableOption "GitOps workflow support";

    branch = lib.mkOption {
      type = lib.types.str;
      description = ''
        Git branch that GitOps tooling should sync from. Used by the ekn CLI
        to generate sync configuration and reference the expected deployment
        branch in commit messages and annotations.
      '';
      example = "production";
    };
  };
}
