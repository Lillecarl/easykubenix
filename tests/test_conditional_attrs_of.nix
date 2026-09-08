let
  sources = import ../nix/sources.nix;
  pkgs = import sources.nixpkgs { };
  # `conditionalAttrsOf`, `mkIfExists` and `mkIfExistsAtPath` live in
  # easykubenix's own `lib.extend` overlay, not plain nixpkgs `lib` -- extend
  # the same way `easykubenix/pkgs/default.nix` does for the real module
  # evaluation.
  lib = pkgs.lib.extend (import ../easykubenix/lib);
  inherit (lib) conditionalAttrsOf mkIfExists mkIfExistsAtPath;

  # A small recursive value type, so a conditional definition can appear at
  # every level. This deliberately does not use `kubeValueType`: this file
  # tests the attribute map on its own, and nothing about Kubernetes lists.
  # The description is fixed here on purpose. A recursive type whose
  # description names its own element type builds an infinite string, and the
  # first thing that renders one is the error message for a rejected
  # definition. `kubeValueType` overrides its description for the same reason.
  valueType =
    (lib.types.oneOf [
      lib.types.bool
      lib.types.int
      lib.types.str
      (conditionalAttrsOf valueType)
    ])
    // {
      description = "test value";
    };

  evalConfiguration =
    modules:
    lib.evalModules {
      modules = [
        {
          options.value = lib.mkOption {
            type = conditionalAttrsOf valueType;
            default = { };
          };
        }
      ]
      ++ modules;
    };

  evalValue = modules: (evalConfiguration modules).config.value;

  # One ordinary definition, then one module of nothing but markers. `absent`
  # has no ordinary definition anywhere, so it must not appear. `present` has
  # one, so the marker content merges into it. The same rule then applies again
  # inside `present.nested`.
  conditionalModules = [
    {
      value.present = {
        original = true;
        nested.existing = "old";
      };
    }
    {
      value = {
        absent = mkIfExists { created = false; };
        present = mkIfExists {
          added = true;
          nested = {
            existing = mkIfExists (lib.mkForce "new");
            missing = mkIfExists "not-created";
          };
        };
      };
    }
  ];

  conditionalEvaluation = evalConfiguration conditionalModules;
  conditionalValues = conditionalEvaluation.config.value;

  # `mkIf false` is not an ordinary definition. A key whose only ordinary
  # definition is switched off must stay absent, even with a marker waiting to
  # patch it.
  falseBase = evalValue [
    { value.onlyConditional = lib.mkIf false { base = true; }; }
    { value.onlyConditional = mkIfExists { patch = true; }; }
  ];

  # A key with no marker anywhere takes the fast path and behaves exactly like
  # plain `attrsOf`: definitions from several modules merge.
  plainMerge = evalValue [
    { value.object.first = true; }
    { value.object.second = true; }
  ];

  # The path form. Every component is conditional, so all three levels must
  # already exist for the content to land.
  pathValues = evalValue [
    {
      value.existing.Deployment.application.replicas = 1;
      value.existing.Deployment."application.with-dot".replicas = 1;
    }
    {
      value = lib.mkMerge [
        (mkIfExistsAtPath
          [
            "existing"
            "Deployment"
            "application"
          ]
          {
            patched = "true";
            replicas = lib.mkForce 4;
          }
        )
        (mkIfExistsAtPath "existing.Deployment.application" { stringPath = "true"; })
        (mkIfExistsAtPath [
          "existing"
          "Deployment"
          "application.with-dot"
        ] { listPath = "true"; })
        (mkIfExistsAtPath [
          "missing"
          "Deployment"
          "application"
        ] { replicas = 10; })
        (mkIfExistsAtPath [
          "existing"
          "MissingKind"
          "application"
        ] { replicas = 10; })
        (mkIfExistsAtPath [
          "existing"
          "Deployment"
          "missing"
        ] { replicas = 10; })
      ];
    }
  ];

  # The following bindings must throw. Each one is exposed as a thunk, so the
  # test can assert on the error without an eager evaluation above.
  emptyPathThrows = mkIfExistsAtPath [ ] { };
  emptyStringComponentThrows = mkIfExistsAtPath "existing..application" { };
  quotedStringPathThrows = mkIfExistsAtPath ''existing."Deployment".application'' { };
  markerAtOptionRootThrows = evalValue [ { value = mkIfExists { anything = true; }; } ];
in
{
  usesV2Merge =
    (conditionalAttrsOf valueType).merge ? v2 && (conditionalAttrsOf valueType).check.isV2MergeCoherent;

  omitsKeysWithoutAnOrdinaryDefinition = !(conditionalValues ? absent);
  mergesIntoExistingKeys = conditionalValues.present;
  respectsNestedExistence = conditionalValues.present.nested;
  falseBaseStaysAbsent = !(falseBase ? onlyConditional);
  plainDefinitionsStillMerge = plainMerge;

  # `valueMeta.attrs` must hold the same keys the value holds, and must recurse.
  exposesMetadataForIncludedKeysOnly =
    conditionalEvaluation.options.value.valueMeta.attrs.present.attrs ? nested
    && !(conditionalEvaluation.options.value.valueMeta.attrs ? absent);

  pathPatchesExistingObject = pathValues.existing.Deployment.application;
  pathListFormSupportsDottedKeys = pathValues.existing.Deployment."application.with-dot";
  pathDoesNotCreateMissingLevels =
    !(pathValues ? missing)
    && !(pathValues.existing ? MissingKind)
    && !(pathValues.existing.Deployment ? missing);

  inherit
    emptyPathThrows
    emptyStringComponentThrows
    quotedStringPathThrows
    markerAtOptionRootThrows
    ;
}
