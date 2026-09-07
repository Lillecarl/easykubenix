let
  # nixidae is the umbrella that holds this repository, and it owns the
  # inputs. Inside it, that is the checkout one directory up. Outside it, it
  # is fetched, and this working copy is put in place of the submodule that
  # came down with it. Either way the answer is the same one, so a build here
  # and a build from the umbrella agree.
  #
  # The input that matters is nanopynix. From the umbrella it is the working
  # copy in the next directory, so a change there is built here with nothing
  # published in between.
  #
  # git+https and not github:, because a GitHub tarball carries no submodule
  # and the siblings are exactly what this is for.
  umbrella =
    if builtins.pathExists ../nix/wire.nix then
      import ../nix/wire.nix
    else
      import (
        (builtins.fetchTree (builtins.parseFlakeRef "git+https://github.com/nixidae/nixidae?submodules=1"))
        .outPath
        + "/nix/wire.nix"
      );

  # Set to make a `--file .` build agree with a flake evaluation. It turns
  # off the overrides the umbrella works through, so going out to fetch one
  # would cost a clone and change nothing.
  overridesDisabled =
    let
      value = builtins.getEnv "FLAKE_COMPATISH_DISABLE_OVERRIDES";
    in
    value != "" && value != "0";
in
{
  inputs ?
    if overridesDisabled then
      (import ./nix/compat.nix).inputs
    else
      umbrella {
        project = "easykubenix";
        source = ./.;
      },
  system ? builtins.currentSystem,
  pkgs ? inputs.nixpkgs.legacyPackages.${system},
  modules ? [ ./demo ],
  specialArgs ? { },
  debug ? null, # unused but kept for API compatibility
}:
let
  pkgs' = pkgs.extend (import ./easykubenix/pkgs/default.nix);
in
let
  pkgs = pkgs';
  lib = pkgs.lib;

  nanopynix = import inputs.nanopynix { inherit pkgs; };
  adios = (import inputs.adios).adios;
  template = definition: adios definition { };
  easykubenix-docs = pkgs.python3Packages.callPackage ./nix/docs.nix { };

  # The `ekn` CLI, built from this repository's own `ekn/` source, which is
  # the only copy of it. `ekn` reads a fixed Nix-to-JSON schema that the
  # modules below produce -- `ekn.eval`'s pydantic models track
  # `kubernetes.nix`, `ekn.gitops` tracks `gitops.nix`, `ekn.validation`
  # tracks `validation.nix` -- so a module change needs a matching `ekn`
  # change, and owning both makes that one commit rather than two
  # repositories and a pin bump between them.
  #
  # `pythonSetWith` and not `pythonSet.overrideScope`. nanopynix builds no
  # `ekn` of its own any more, so `kr8s` -- which nothing else declares -- is
  # not in its closure, and an overlay cannot put it there: a set lifts its
  # nixpkgs packages once, from the roots it was seeded with, and the lifting
  # machinery is private to nanopynix' nix/python-set.nix. Passing `./ekn` as
  # a project root is what gets ekn's own dependencies resolved alongside
  # nanopynix' -- `overrideScope` here fails with `attribute 'kr8s' missing`.
  eknPythonSet = nanopynix.pythonSetWith {
    projectRoots = [ ./ekn ];
    overlay = pySelf: _pyPrev: {
      ekn = pySelf.callPackage (nanopynix.ps.mkProject {
        projectRoot = ./ekn;
        inherit (nanopynix.pythonSet) python;
        # No `+nix<version>` local version segment, unlike nanopynix' own
        # projects. That segment tells apart the same source built against
        # each supported Nix version; this repository has exactly one
        # nanopynix, so it would distinguish nothing.
        extra = rendered: {
          meta = rendered.meta // {
            platforms = lib.platforms.unix;
          };
        };
      }) { };
    };
  };

  # `caBundle` is not optional. `internal.nix` and `lib/parseYamlStream.nix`
  # run this program inside a Nix build sandbox, which sets `SSL_CERT_FILE` to
  # a path that deliberately does not exist -- and `ekn` imports pygit2, which
  # initialises OpenSSL at import and refuses to start with no trust store.
  # See nanopynix' nix/mk-app.nix, and the `ekn-sandbox` gate in nix/default.nix
  # that this repository keeps over it.
  eknCli = nanopynix.mkApp {
    name = "ekn";
    pythonSet = eknPythonSet;
    # `true`, and not an environment variable. `mkApp` renders the three
    # completion scripts from argcomplete's own `shell_integration.shellcode`,
    # which needs the name of the program and nothing else. The clypi shape
    # this replaces named the variable clypi looked for.
    completions = true;
    caBundle = true;

    # `ekn/src/ekn/sops.py` runs both of these as subprocesses: `age-keygen`
    # to mint a keypair for each `kubernetes.sopsAgeIdentities` entry that is
    # missing one, and `sops` to encrypt and decrypt with it. They are
    # implementation details of this program, not tools a caller chooses, so
    # they belong in its own closure.
    #
    # Until now they came from the ambient PATH, which meant `ekn kubeapply`
    # worked only where someone happened to have installed them -- the dev
    # shell below lists `age` for exactly that reason. On a machine without
    # them the bootstrap died mid-apply with
    # `FileNotFoundError: [Errno 2] No such file or directory: 'age-keygen'`,
    # after it had already reached the cluster.
    #
    # `kubectl` and `git`, which the CLI also shells out to, are deliberately
    # NOT here: those are the operator's own tools, with the operator's own
    # config and credential helpers behind them.
    pathInputs = [
      pkgs.age
      pkgs.sops
    ];
  };

  # The environment the test suite and the dev shell run in: one venv holding
  # `ekn`'s whole dependency closure plus `nanopynix`'s `test` extra, which is
  # what supplies pytest and anyio.
  #
  # This used to be nanopynix' exported `pynixDevEnv`, which worked only for
  # as long as nanopynix declared `ekn` itself -- that is where `kr8s` came
  # from. It no longer does, so this repository assembles the environment for
  # its own project, which is where that belongs.
  #
  # `ekn` is a built install here, not an editable one, and that is not a
  # regression: pytest.ini's `pythonpath = ekn/src` is inserted at the front
  # of `sys.path` and shadows it, so the tests read the working tree either
  # way. `pynix` is deliberately absent -- nothing here imports it.
  eknDevEnv = eknPythonSet.mkVirtualEnv "easykubenix-dev-env" {
    ekn = [ ];
    nanopynix = [ "test" ];
    nanopynix-helpers = [ ];
    pytest-agent = [ ];
  };

  # The modules every instance is evaluated with -- the top-level one, and
  # every nested one a GitOps target instantiates through `mkInstance` below.
  # One list, deliberately: a nested instance is a whole easykubenix
  # configuration rather than a cut-down one, so a bootstrap target can render
  # a Helm chart (which is how you install the GitOps engine it exists to
  # bootstrap). Nix's laziness means the modules it never uses cost it nothing.
  baseModules = [
    ./easykubenix/assertions.nix
    ./easykubenix/ekn.nix
    ./easykubenix/gitops.nix
    ./easykubenix/helm.nix
    ./easykubenix/importyaml.nix
    ./easykubenix/internal.nix
    ./easykubenix/kluctl.nix
    ./easykubenix/kubernetes.nix
    ./easykubenix/lib.nix
    ./easykubenix/validation.nix
  ];

  # Instantiate an easykubenix configuration. Called once below for the
  # top-level instance, and again by gitops.nix for each GitOps target that
  # carries its own `modules` -- see `ekn.lib.mkInstance` in `moduleArgs`.
  #
  # Everything costly is already bound in this `let` and is captured by
  # closure rather than recomputed: the extended `pkgs`, the `nanopynix`
  # import, `eknCli`, the `ekn` helper set. What a nested instance does cost
  # is one more option merge; nothing in it is forced until something reads
  # its `kubernetes.generated`, and `apiResources/v1.33.json` is imported by
  # path, so Nix caches that parse across instances.
  #
  # Returns `evalModules`' own result (`.config`, `.options`), not the
  # attrset this file returns -- a nested instance has no use for
  # `validationScript`/`manifestJSONFile`, and building them would force work
  # nobody asked for.
  mkInstance =
    {
      modules ? [ ],
      specialArgs ? { },
    }:
    lib.evalModules {
      specialArgs = {
        inherit adios template;
      }
      // specialArgs;

      modules = [ { _module.args = moduleArgs; } ] ++ baseModules ++ modules;
    };

  moduleArgs = {
    inherit pkgs;
    inherit (pkgs) lib;
    # The `ekn` CLI package itself, under a distinct name so it can't
    # be conflated with the `ekn` module-arg below (GitOps helpers) or
    # `object.ekn` (GitOps-routing metadata, see kubernetes.nix's
    # `ekn.gitOpsTarget`/`gitopsTargets`) -- same word, three unrelated
    # meanings in this codebase, kept in separate namespaces. Built from
    # this repository's own `ekn/` source (see `eknCli` above); an
    # internal build-time tool for `internal.nix` and for
    # parseYamlStream.nix's IFD fallback below.
    eknPackage = eknCli;
    # Helper functions for consuming modules, distinct from `object.ekn`
    # (GitOps-routing metadata on rendered Kubernetes objects, see
    # kubernetes.nix's `ekn.gitOpsTarget`/`gitopsTargets`) and the
    # `ekn` CLI package bound above as `eknPackage` -- same word, three
    # unrelated meanings in this codebase, kept in separate namespaces
    # by construction (module function-arg vs. object attrset field vs.
    # outer let-binding), but worth this note so nobody conflates them.
    ekn = {
      # Recursively wrap every leaf of an attrset in `lib.mkDefault`,
      # so a module's baked-in option value (e.g. a Helm chart's
      # `values`) stays overridable leaf-by-leaf instead of a caller's
      # single definition replacing the whole thing.
      mkDefaults = lib.mapAttrsRecursive (_: v: lib.mkDefault v);
      lib = {
        # Evaluate a whole separate easykubenix configuration from inside
        # this one. Used by gitops.nix for `gitOps.targets.<name>.modules`;
        # see `mkInstance` above for what it does and does not share with
        # the instance calling it.
        inherit mkInstance;
        # ArgoCD's per-object `argocd.argoproj.io/tracking-id` value, for a
        # `gitOps.targets.<name>.annotations` entry -- the one piece of
        # controller-specific knowledge here, kept as an opt-in helper so
        # `gitOps.targets` itself stays neutral. See argocdTrackingId.nix.
        argocdTrackingId = import ./easykubenix/lib/argocdTrackingId.nix { inherit lib; };
        # Recursive JSON-ish value type (drop-in replacement for
        # `pkgs.formats.json{}.type`/`settingsFormat.type`) that also
        # accepts an attrset-keyed-by-`name` shorthand anywhere a list
        # of named things (containers, ownerReferences, ...) would go,
        # merged via ordinary `attrsOf` semantics and always emitted
        # back out as a real list. See kubeValueType.nix.
        kubeValueType = import ./easykubenix/lib/kubeValueType.nix { inherit lib; };
        # Shared YAML-stream parser (primop path + `ekn _yamlToJson`
        # derivation fallback) used by importyaml.nix and helm.nix so
        # neither hand-rolls the primop-vs-CLI-fallback dispatch. See
        # parseYamlStream.nix.
        parseYAMLStream = import ./easykubenix/lib/parseYamlStream.nix {
          inherit lib pkgs;
          eknPackage = eknCli;
        };
        # The write direction, same dispatch (primop path + `ekn
        # _jsonToYAML` derivation fallback). Drop-in for
        # `builtins.toYAML`, which is a nanopynix primop and therefore
        # absent under stock Nix -- a consumer calling it directly
        # cannot be evaluated by plain `nix eval` at all, it dies with
        # `attribute 'toYAML' missing`. See serialiseYaml.nix.
        toYAML = import ./easykubenix/lib/serialiseYaml.nix {
          inherit lib pkgs;
          eknPackage = eknCli;
        };
      };
    };
  };

  eval = mkInstance { inherit modules specialArgs; };
in
{
  inherit (eval.config.internal)
    manifestAttrs
    manifestJSON
    manifestJSONFile
    manifestYAML
    manifestYAMLFile
    manifestYAMLList
    manifestYAMLFileList
    manifestYAMLDir
    ;

  inherit (eval) config;

  deploymentScript = eval.config.kluctl.script;
  validationScript = eval.config.validation.script;
  passthru = {
    # `grpclib-transports` was here too, but nanopynix vendors that project
    # now (it is no longer a flake input) and stopped exposing it as a
    # top-level attribute, so the passthru entry would throw when forced.
    inherit (nanopynix)
      nanopynix
      nanopynix-bindings
      nanopynix-helpers
      ;
    inherit
      inputs
      pkgs
      lib
      adios
      eval
      easykubenix-docs
      ;
    # This repository's own build of the CLI, from `ekn/`. nanopynix builds no
    # `ekn` at all any more -- see `eknPythonSet` above.
    ekn = eknCli;
    inherit eknDevEnv;
  };
}
