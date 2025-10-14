{
  config,
  pkgs,
  lib,
  ...
}:
let
  cfg = config.kluctl;
  settingsFormat = pkgs.formats.json { };

  writeMultipleFiles =
    {
      name,
      files,
      extraCommands ? "",
    }:
    let
      fileList = lib.mapAttrsToList (path: file: {
        inherit path;
        content = file.content or file;
        mode = if file.executable or false then "755" else file.mode or "644";
      }) files;

      # Create attribute names for passAsFile
      passAsFileAttrs = builtins.listToAttrs (
        lib.imap0 (i: file: {
          name = "file${toString i}";
          value = file.content;
        }) fileList
      );

      passAsFileNames = builtins.attrNames passAsFileAttrs;

      commands =
        (lib.imap0 (i: file: ''
          mkdir -p $out/$(dirname "${file.path}")
          cp "$file${toString i}Path" $out/${file.path}
          chmod ${file.mode} $out/${file.path}
        '') fileList)
        ++ (lib.toList extraCommands);

    in
    pkgs.runCommand name (
      passAsFileAttrs
      // {
        passAsFile = passAsFileNames;
      }
    ) (builtins.concatStringsSep "\n" commands);
in
{
  options = {
    kluctl = {
      package = lib.mkPackageOption pkgs "kluctl" { };
      discriminator = lib.mkOption {
        type = lib.types.str;
        description = "kluctl deployment label discriminator, AKA K8s label conformant project name";
        default = "easykubenix";
      };
      project = lib.mkOption {
        type = settingsFormat.type;
        description = "Anything to be rendered into .kluctl.yaml";
        default = {
          targets = [ { name = "local"; } ];
        };
      };
      resourcePriority = lib.mkOption {
        type = lib.types.attrsOf lib.types.int;
        description = "Priority of which order to apply resource types in";
        default = {
          CustomResourceDefinition = 10;
          Namespace = 20;
        };
      };
      deployment = lib.mkOption {
        type = settingsFormat.type;
        description = "Anything to be rendered into deployment.yaml";
        default = { };
      };
      files = lib.mkOption {
        type = lib.types.attrsOf lib.types.str;
        description = "Attribute set where name is filename and value is string to be put into the file";
        default = { };
      };
      retryCount = lib.mkOption {
        type = lib.types.int;
        default = 3;
        apply = toString;
      };
      projectDir = lib.mkOption {
        type = lib.types.package;
        internal = true;
      };
      script = lib.mkOption {
        type = lib.types.package;
        internal = true;
      };
    };
  };
  config = {
    kluctl.deployment = {
      deployments =
        (lib.pipe cfg.resourcePriority [
          lib.attrsToList
          (lib.sort (a: b: a.value < b.value))
          (lib.map (v: {
            path = v.name;
            barrier = true;
          }))
        ])
        ++ [
          {
            path = "default";
          }
        ];
    };
    kluctl.projectDir = writeMultipleFiles {
      name = "kluctlProject";
      files = {
        ".kluctl.yaml" = {
          content = builtins.toJSON config.kluctl.project;
        };
        "deployment.yaml" = {
          content = builtins.toJSON config.kluctl.deployment;
        };
        # Don't apply prioritized resources again.
        "default/easykubenix.yaml" = {
          content = builtins.toJSON {
            apiVersion = "v1";
            kind = "List";
            items = lib.filter (
              v: !lib.elem (v.kind or null) (lib.attrNames cfg.resourcePriority)
            ) config.kubernetes.generated;
          };
        };
      }
      # Prioritized resources
      // (lib.mapAttrs' (n: v: {
        name = "${n}/easykubenix.yaml";
        value = builtins.toJSON {
          apiVersion = "v1";
          kind = "List";
          items = lib.filter (v: v.kind or null == n) config.kubernetes.generated;
        };
      }) cfg.resourcePriority)
      # Other user-supplied files
      // cfg.files;
    };
    kluctl.script =
      pkgs.writeScriptBin "kubenixDeploy" # fish
        ''
          #! ${lib.getExe pkgs.fishMinimal}
          echo "Calling kluctl with ${cfg.retryCount} retries."
          set command ${lib.getExe cfg.package} deploy \
              --no-update-check \
              --target local \
              --discriminator ${cfg.discriminator} \
              --project-dir ${cfg.projectDir} \
              $argv # --dry-run? --yes? --prune!
          echo $command

          for i in (seq ${cfg.retryCount})
            $command && begin
              echo "Great success!"
              exit 0
            end
          end
          exit $status
        '';
  };
}
