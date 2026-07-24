{
  inputs ? (import ./nix/compat.nix).inputs,
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
    completions.var = "_EKN_COMPLETE";
    caBundle = true;
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

  eval = lib.evalModules {
    specialArgs = {
      inherit adios template;
    }
    // specialArgs;

    modules = [
      {
        _module.args = {
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
            };
          };
        };
      }
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
    ]
    ++ modules;
  };
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
