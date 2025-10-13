{
  config,
  pkgs,
  lib,
  ...
}:
{
  options.internal = lib.mkOption {
    type = lib.types.anything;
  };
  config.internal = rec {
    manifestAttrs = {
      apiVersion = "v1";
      kind = "List";
      items = config.kubernetes.generated;
    };
    manifestJSON = builtins.toJSON manifestAttrs;
    manifestJSONFile = pkgs.writeText "manifest.json" manifestJSON;
    # Beware that YAML rendering requires IFD
    manifestYAML = builtins.readFile manifestYAMLFile;
    manifestYAMLFile =
      pkgs.runCommandNoCCLocal "manifest.yaml" { } # bash
        ''
          ${lib.getExe pkgs.yq} --yaml-output '.' ${manifestJSONFile} > $out
        '';
  };
}
