let
  compat = import ../nix/compat.nix;
  pkgs = import compat.inputs.nixpkgs { };
  lib = pkgs.lib;
  adios = (import compat.inputs.adios).adios;
  template = definition: adios definition { };

  arguments = index: {
    name = "database-${toString index}";
    namespace = "default";
    encryptedData = {
      password = "AgByEncryptedPassword-${toString index}";
      username = "AgByEncryptedUsername-${toString index}";
    };
    labels = {
      app = "api";
      component = "database";
    };
    annotations = {
      "reloader.stakater.com/auto" = "true";
    };
  };

  render = args: {
    apiVersion = "bitnami.com/v1alpha1";
    kind = "SealedSecret";
    metadata = {
      inherit (args) name namespace labels annotations;
    };
    spec = {
      inherit (args) encryptedData;
      template = {
        metadata = {
          inherit (args) labels annotations;
        };
        type = "Opaque";
      };
    };
  };

  evalModulesTemplate = index:
    let
      cfg = (lib.evalModules {
        modules = [
          ({ config, lib, ... }: {
            options = {
              name = lib.mkOption { type = lib.types.str; };
              namespace = lib.mkOption { type = lib.types.str; };
              encryptedData = lib.mkOption { type = lib.types.attrsOf lib.types.str; };
              labels = lib.mkOption { type = lib.types.attrsOf lib.types.str; };
              annotations = lib.mkOption { type = lib.types.attrsOf lib.types.str; };
              result = lib.mkOption { type = lib.types.attrs; readOnly = true; };
            };
            config.result = render config;
          })
          { config = arguments index; }
        ];
      }).config;
    in
    cfg.result;

  adiosDefinition = {
    name = "sealedSecret";
    options = {
      name.type = adios.types.str;
      namespace.type = adios.types.str;
      encryptedData.type = adios.types.attrsOf adios.types.str;
      labels.type = adios.types.attrsOf adios.types.str;
      annotations.type = adios.types.attrsOf adios.types.str;
    };
    impl = { options }: render options;
  };

  adiosTemplate = template adiosDefinition;
  adiosTemplateBuild = index: (template adiosDefinition) (arguments index);

  baseline = lib.types.int.name;
  force = values: builtins.deepSeq baseline (builtins.deepSeq values "ok");
in
{
  baseline = force [ ];
  evalModules100 = force (builtins.genList evalModulesTemplate 100);
  adiosCalls100 = force (builtins.genList (index: adiosTemplate (arguments index)) 100);
  adiosBuilds100 = force (builtins.genList adiosTemplateBuild 100);
  evalModules10000 = force (builtins.genList evalModulesTemplate 10000);
  adiosCalls10000 = force (builtins.genList (index: adiosTemplate (arguments index)) 10000);
  adiosBuilds10000 = force (builtins.genList adiosTemplateBuild 10000);

  l2 = {
    evalModules = index: builtins.deepSeq (evalModulesTemplate index) null;
    adiosCalls = index: builtins.deepSeq (adiosTemplate (arguments index)) null;
    adiosBuilds = index: builtins.deepSeq (adiosTemplateBuild index) null;
  };
}
