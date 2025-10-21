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
    # Makes a valid YAML string, supports multiple documents or single attrsets,
    # documents will be JSON formatted since Nix can't render YAML.
    toYAMLStr =
      input:
      if builtins.typeOf input == "list" then
        lib.concatStringsSep "\n---\n" (map (doc: builtins.toJSON doc) input)
      else if builtins.typeOf input == "set" then
        builtins.toJSON input
      else
        throw "toYAML only supports set and list types";

    # Makes a valid YAML string, supports multiple documents or single attrsets,
    # reformatted using "yq-go".
    toYAMLFile =
      filename: input:
      pkgs.runCommand filename
        {
          nativeBuildInputs = [
            pkgs.yq-go
          ];
          yamlContent = toYAMLStr input;
          passAsFile = [ "yamlContent" ];
        }
        #bash
        ''
          yq --prettyPrint < $yamlContentPath > $out
        '';

    manifestAttrs = {
      apiVersion = "v1";
      kind = "List";
      items = config.kubernetes.generated;
    };
    manifestJSON = builtins.toJSON manifestAttrs;
    manifestJSONFile = pkgs.writeText "manifest.json" manifestJSON;
    # Beware that YAML rendering requires IFD
    manifestYAMLList = builtins.readFile manifestYAMLFile;
    manifestYAMLFileList =
      pkgs.runCommandNoCCLocal "manifest.yaml" { } # bash
        ''
          ${lib.getExe pkgs.yq} --yaml-output '.' ${manifestJSONFile} > $out
        '';
    manifestYAML = builtins.readFile manifestYAMLFile;
    manifestYAMLFile = toYAMLFile "manifest.yaml" config.kubernetes.generated;
  };
}
