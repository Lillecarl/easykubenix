{
  config,
  pkgs,
  lib,
  ...
}:
let
  cfg = config.tofu;

  # Same guard the Kubernetes side puts on every fully-rendered output: force
  # `config.assertions` and `config.warnings` before handing anything back, so
  # a failed assertion surfaces as its own message rather than as whatever
  # downstream error the bad value happens to cause.
  checked = lib.asserts.checkAssertWarn config.assertions config.warnings;

  # A JSON value, recursively. Deliberately not `ekn.lib.kubeValueType`: that
  # one also accepts an attrset keyed by `name` wherever a list of named
  # things would go, which is a Kubernetes shape and means nothing here.
  #
  # `lib.types.anything` would merge these too, but it merges an attrset by
  # recursing into every key looking for mkIf/mkMerge markers, and a provider
  # block is deep. This is the same recursive type nixpkgs' `formats.json`
  # builds, declared here because the module wants no file-format machinery
  # around it.
  valueType =
    with lib.types;
    let
      inner = nullOr (oneOf [
        bool
        int
        float
        str
        path
        (attrsOf inner)
        (listOf inner)
      ]);
    in
    inner;

  block =
    description:
    lib.mkOption {
      type = lib.types.attrsOf valueType;
      default = { };
      inherit description;
    };

  listBlock =
    description:
    lib.mkOption {
      type = lib.types.listOf valueType;
      default = [ ];
      inherit description;
    };

  # OpenTofu reads `null` as a set value, not as an absent one, and a provider
  # that receives an explicit null for an optional argument behaves differently
  # from one that receives nothing. The module system produces nulls freely --
  # every `nullOr` option that nobody set -- so they are dropped here rather
  # than left for each author to remember.
  #
  # An empty attrset is kept. `resource.aws_s3_bucket.x = { }` is a real
  # resource with every argument defaulted.
  dropNulls =
    value:
    if lib.isAttrs value && !(lib.isDerivation value) then
      lib.mapAttrs (_: dropNulls) (lib.filterAttrs (_: v: v != null) value)
    else if lib.isList value then
      map dropNulls (lib.filter (v: v != null) value)
    else
      value;
in
{
  _class = "tf";

  options.tofu = {
    package = lib.mkPackageOption pkgs "opentofu" { };

    providers = lib.mkOption {
      type = lib.types.functionTo (lib.types.listOf lib.types.package);
      default = _plugins: [ ];
      description = ''
        Providers this configuration needs, selected from
        `tofu.package.plugins`, in the shape `opentofu.withPlugins` takes.

        Pinning providers with Nix rather than with a lockfile is the point.
        `wrappedPackage` below resolves every plugin from the store, so
        `tofu init` needs no network and cannot drift between two runs of the
        same evaluation. nixpkgs tests exactly this -- see
        `opentofu_plugins_test` in its opentofu package, which runs `tofu init`
        with the proxy pointed at a dead port.

        A provider still has to be declared to OpenTofu as well, in
        `tofu.terraform.required_providers`. This option only decides which
        binaries exist.
      '';
      example = lib.literalExpression "plugins: [ plugins.hashicorp_random ]";
    };

    wrappedPackage = lib.mkOption {
      type = lib.types.package;
      readOnly = true;
      description = ''
        `tofu.package` with `tofu.providers` wrapped in, which is the binary
        anything applying this configuration must run. Calling plain
        `tofu.package` instead leaves `tofu init` reaching for the registry.
      '';
    };

    terraform = block ''
      The `terraform` block: `required_version`, `required_providers`, and the
      `backend` this configuration keeps its state in.

      There is no default backend. OpenTofu falls back to a local state file
      beside the working directory, which is almost never what a deployment
      unit wants -- see `design/opentofu.md` on one state file per unit.
    '';

    provider = block ''
      Provider configuration, keyed by provider name. A list of attrsets
      instead of one attrset configures several aliases of the same provider.

      Do not put credentials here. This attrset is rendered into
      `/nix/store`, which is world-readable.
    '';

    resource = block ''
      Resources, keyed by type and then by name:
      `tofu.resource.aws_instance.web = { ... }`.
    '';

    data = block ''
      Data sources, keyed by type and then by name. Same shape as `resource`.
    '';

    module = block ''
      OpenTofu modules, keyed by name. Unrelated to the Nix module system:
      this is OpenTofu's own `source`-and-inputs mechanism.
    '';

    output = block ''
      Outputs, keyed by name. `tofu output -json` is what reads these back.
    '';

    variable = block ''
      Input variables, keyed by name. Mostly redundant here -- Nix already
      computes what a variable would -- except for a value that must not
      reach `/nix/store`, which a variable takes at apply time instead.
    '';

    locals = block ''
      File-scoped values, keyed by name. Nix computes these better; the option
      exists because a rendered OpenTofu module may still want them.
    '';

    check = block ''
      Check blocks, keyed by name.
    '';

    ephemeral = block ''
      Ephemeral resources, keyed by type and then by name. These are never
      written to state.
    '';

    import = listBlock ''
      Import blocks -- each an attrset with `to` and `id`.
    '';

    moved = listBlock ''
      Moved blocks -- each an attrset with `from` and `to`.
    '';

    removed = listBlock ''
      Removed blocks -- each an attrset with `from` and a `lifecycle`.
    '';

    generated = lib.mkOption {
      type = lib.types.anything;
      readOnly = true;
      description = ''
        The complete OpenTofu configuration as one attrset, nulls dropped and
        empty blocks omitted. This is `config.tf.json`'s content.
      '';
    };

    resolvedProviders = lib.mkOption {
      type = lib.types.anything;
      readOnly = true;
      description = ''
        Every provider `tofu.providers` selected, with the version and store
        path Nix resolved it to.

        This exists because `.terraform.lock.hcl` does not. The lock's hashes
        verify a property the store already guarantees, so dropping it loses
        no integrity -- but it did carry one thing worth keeping: notice that
        a provider moved. `config.tf.json` records only the
        `required_providers` *constraints*, so a 5.31 to 5.40 bump can pass
        through a reviewed diff invisibly, the whole change being a store path
        nobody wrote down.

        Rendered beside `config.tf.json` as `providers.json` and committed
        with it, so the bump shows up in review rather than at apply time.
      '';
    };

    configFile = lib.mkOption {
      type = lib.types.package;
      readOnly = true;
      description = ''
        A directory holding `config.tf.json` and `providers.json`. A directory
        and not a file, because `tofu` takes a working directory rather than a
        config path.
      '';
    };
  };

  config.tofu = {
    wrappedPackage = cfg.package.withPlugins cfg.providers;

    resolvedProviders = map (plugin: {
      inherit (plugin) version;
      name = plugin.pname or plugin.name;
      path = "${plugin}";
    }) (cfg.providers cfg.package.plugins);

    generated = checked (
      lib.filterAttrs (_: value: value != { } && value != [ ]) (dropNulls {
        inherit (cfg)
          terraform
          provider
          resource
          data
          module
          output
          variable
          locals
          check
          ephemeral
          import
          moved
          removed
          ;
      })
    );

    # Pretty-printed, and that is not cosmetic: `ekn commit` writes this file
    # to a branch so a person can read the diff. `builtins.toJSON` emits one
    # line, and a one-line diff of a whole infrastructure change says only that
    # it changed.
    #
    # `jq` rather than a Nix printer, because Nix has none. `--sort-keys` on
    # top of the alphabetical order `builtins.toJSON` already produces, so the
    # order is jq's own rule rather than a coincidence of how Nix serializes.
    configFile =
      pkgs.runCommand "tofu-config"
        {
          nativeBuildInputs = [ pkgs.jq ];
          value = cfg.generated;
          providers = cfg.resolvedProviders;
          __structuredAttrs = true;
          preferLocalBuild = true;
        }
        ''
          mkdir -p "$out"
          jq --sort-keys .value "$NIX_ATTRS_JSON_FILE" > "$out/config.tf.json"
          jq --sort-keys .providers "$NIX_ATTRS_JSON_FILE" > "$out/providers.json"
        '';
  };
}
