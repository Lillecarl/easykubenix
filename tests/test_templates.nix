let
  compat = import ../nix/compat.nix;
  pkgs = import compat.inputs.nixpkgs { };
  easy = import ../. {
    inherit pkgs;
    modules = [
      (
        {
          adios,
          config,
          template,
          ...
        }:
        {
          kubernetes.templates.sealedSecret =
            template {
              name = "sealedSecret";
              options = {
                encryptedData = {
                  type = adios.types.attrsOf adios.types.str;
                };
                template = {
                  type = adios.types.attrs;
                  default = { };
                };
              };
              impl =
                { options }:
                {
                  apiVersion = "bitnami.com/v1alpha1";
                  spec = {
                    inherit (options) encryptedData template;
                  };
                };
            };

          kubernetes.objects.default.SealedSecret.database =
            config.kubernetes.templates.sealedSecret {
              encryptedData.password = "AgByEncrypted";
              template.metadata.labels.app = "api";
            };
        }
      )
    ];
  };
in
{
  templates = easy.config.kubernetes.generatedByPath.default.SealedSecret.database;
  invalidTemplateArguments = easy.config.kubernetes.templates.sealedSecret {
    encryptedData = "not an attribute set";
  };
}
