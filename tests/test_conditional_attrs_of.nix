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

  # ---------------------------------------------------------------------
  # Priorities, on an ordinary definition and inside marker content.
  #
  # `mergeEntry` decides existence and then hands the surviving definitions to
  # `elemType`, so the ordinary module rules have to keep working on both sides
  # of that split.
  # ---------------------------------------------------------------------

  # Two markers for one existing key. Both contribute; neither is a definition
  # that makes the key exist.
  twoMarkersOnOneKey = evalValue [
    { value.present.base = true; }
    { value.present = mkIfExists { first = 1; }; }
    { value.present = mkIfExists { second = 2; }; }
  ];

  # A marker whose content is `mkDefault` loses to the ordinary definition, and
  # still fills a field the ordinary definition leaves alone. The content is
  # merged by the normal rules, so a priority inside it behaves normally.
  markerContentDefaults = evalValue [
    {
      value.present = {
        replicas = 1;
      };
    }
    {
      value.present = mkIfExists {
        replicas = lib.mkDefault 9;
        paused = lib.mkDefault true;
      };
    }
  ];

  # `mkIf true` is an ordinary definition, so the key exists and the marker
  # lands. The `mkIf false` half of this pair is `falseBase` above.
  trueBaseExists = evalValue [
    { value.conditionalBase = lib.mkIf true { base = true; }; }
    { value.conditionalBase = mkIfExists { patch = true; }; }
  ];

  # A key whose ordinary definition is a scalar. `mergeEntry` takes the slow
  # path here, because the marker carries a `_type`, and the content has to
  # merge against a non-attribute value like any other definition.
  markerOnAScalarKey = evalValue [
    { value.replicas = 1; }
    { value.replicas = mkIfExists (lib.mkForce 3); }
  ];

  # A key that only a marker defines, where the marker content is itself a
  # scalar. Still absent -- the content never gets to make the key exist.
  markerOnlyScalarKeyStaysAbsent = evalValue [
    { value.other = 1; }
    { value.missing = mkIfExists 5; }
  ];

  # The fast path in `mergeEntry` skips the collector when no definition of a
  # key carries a `_type`. A `mkForce` carries one, so this key takes the slow
  # path instead -- and must still merge exactly as `attrsOf` would.
  forcedOrdinaryDefinitionTakesTheSlowPath = evalValue [
    { value.present.replicas = 1; }
    { value.present.replicas = lib.mkForce 3; }
  ];

  # `mkIf` around a marker changes no priority, so it stays legal. It is the
  # ordinary way to write a patch that is itself conditional.
  conditionalMarkerIsAllowed = evalValue [
    { value.present.base = true; }
    {
      value.present = lib.mkIf true (mkIfExists {
        patch = true;
      });
    }
  ];

  conditionalMarkerSwitchedOff = evalValue [
    { value.present.base = true; }
    {
      value.present = lib.mkIf false (mkIfExists {
        patch = true;
      });
    }
  ];

  # The following bindings must throw. Each one is exposed as a thunk, so the
  # test can assert on the error without an eager evaluation above.

  # A priority around the marker rather than inside its content. Refused,
  # because it cannot work in either direction: a higher priority outranks the
  # ordinary definitions and the marker then deletes the key it meant to
  # patch, and a lower one is itself dropped.
  forcedMarkerThrows = evalValue [
    { value.present.base = true; }
    {
      value.present = lib.mkForce (mkIfExists {
        patch = true;
      });
    }
  ];

  # The other direction, which merely does nothing. Refused by the same rule,
  # rather than left as a definition that silently never applies.
  defaultedMarkerThrows = evalValue [
    { value.present.base = true; }
    {
      value.present = lib.mkDefault (mkIfExists {
        patch = true;
      });
    }
  ];

  # The wrappers nest, so the check follows them. `mkIf` is not itself an
  # offence, and an override under one still is.
  markerForcedUnderAnMkIfThrows = evalValue [
    { value.present.base = true; }
    {
      value.present = lib.mkIf true (
        lib.mkForce (mkIfExists {
          patch = true;
        })
      );
    }
  ];

  # `mkMerge` too, on either side.
  markerForcedInsideAnMkMergeThrows = evalValue [
    { value.present.base = true; }
    {
      value.present = lib.mkMerge [
        { other = true; }
        (lib.mkForce (mkIfExists {
          patch = true;
        }))
      ];
    }
  ];

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
    twoMarkersOnOneKey
    markerContentDefaults
    trueBaseExists
    markerOnAScalarKey
    forcedOrdinaryDefinitionTakesTheSlowPath
    conditionalMarkerIsAllowed
    conditionalMarkerSwitchedOff
    ;
  markerOnlyScalarKeyStaysAbsent = !(markerOnlyScalarKeyStaysAbsent ? missing);

  inherit
    forcedMarkerThrows
    defaultedMarkerThrows
    markerForcedUnderAnMkIfThrows
    markerForcedInsideAnMkMergeThrows
    emptyPathThrows
    emptyStringComponentThrows
    quotedStringPathThrows
    markerAtOptionRootThrows
    ;
}
