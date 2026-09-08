let
  sources = import ../nix/sources.nix;
  pkgs = import sources.nixpkgs { };
  # `mkNamedList`/`mkNumberedList` live in easykubenix's own `lib.extend`
  # overlay, not plain nixpkgs `lib` -- extend the same way
  # `easykubenix/pkgs/default.nix` does for the real module evaluation.
  lib = pkgs.lib.extend (import ../easykubenix/lib);
  kubeValueType = import ../easykubenix/lib/kubeValueType.nix { inherit lib; };

  # Evaluate `kubeValueType` at the root of a tiny module tree, so `merge`
  # sees the same shape (`loc`, multiple defs across modules) it will see
  # for real once wired into kubernetes.nix's `spec`/`data`/etc.
  evalValue =
    modules:
    (lib.evalModules {
      modules = [
        {
          options.value = lib.mkOption {
            type = kubeValueType;
            default = null;
          };
        }
      ]
      ++ modules;
    }).config.value;

  # A plain rendered list (e.g. straight from a Helm chart, never
  # pre-converted) merged against a LATER module's explicit
  # `lib.mkNamedList` override of one entry -- this is the only case the
  # "merging conversion" should ever kick in for.
  namedListOverrideViaMkNamedList = evalValue [
    {
      value.template.spec.containers = [
        {
          name = "app";
          image = "v1";
        }
        {
          name = "sidecar";
          image = "s1";
        }
      ];
    }
    {
      value.template.spec.containers = lib.mkNamedList {
        app.image = lib.mkForce "v2";
      };
    }
  ];

  # A plain, ordinary list with NO mkNamedList/mkNumberedList involved
  # anywhere must never be silently reinterpreted -- even though every
  # element has a unique `name`, this must behave exactly like plain
  # `listOf`: a single list definition, untouched.
  plainListOfNamedThingsNeverAutoConverted = evalValue [
    {
      value.containers = [
        {
          name = "app";
          image = "v1";
        }
      ];
    }
  ];

  # An ordinary, UNMARKED attrset must be REJECTED for a field that's
  # given a real list elsewhere -- Kubernetes fields have one fixed shape,
  # so silently reinterpreting an unmarked attrs mistake as "the attrs
  # form of a list" would hide a real bug.
  unmarkedAttrsRejectedAgainstListThrows = evalValue [
    {
      value.containers = [
        {
          name = "app";
          image = "v1";
        }
      ];
    }
    {
      value.containers = {
        name = "oops";
        image = "v2";
      };
    }
  ];

  # `ownerReferences`-style lists: two elements can legitimately share a
  # `name` across different `kind`s. Since nothing here uses
  # mkNamedList/mkNumberedList, this must stay a plain list untouched --
  # no risk of `listToAttrs`-style silent dedup, because no by-name
  # merging is ever attempted without an explicit marker.
  ownerReferencesWithDuplicateNamesPreserved = evalValue [
    {
      value.metadata.ownerReferences = [
        {
          kind = "ConfigMap";
          name = "prod";
          uid = "a";
        }
        {
          kind = "Deployment";
          name = "prod";
          uid = "b";
        }
      ];
    }
  ];

  initContainersOverrideViaMkNumberedList = evalValue [
    {
      value.initContainers = [
        {
          name = "migrate";
          image = "m1";
        }
        {
          name = "wait-for-db";
          image = "w1";
        }
      ];
    }
    {
      value.initContainers = lib.mkNumberedList {
        "0".image = lib.mkForce "m2";
      };
    }
  ];

  plainListPassthrough = evalValue [
    {
      value.args = [
        "--foo"
        "--bar"
      ];
    }
  ];

  nestedAttrsMergeAcrossModules = evalValue [
    { value.spec.foo = "a"; }
    { value.spec.bar = "b"; }
  ];

  scalarsPassthrough = evalValue [
    {
      value = {
        aBool = true;
        anInt = 3;
        aFloat = 1.5;
        aString = "hello";
        aNull = null;
      };
    }
  ];

  multiDefPlainListConcatenates = evalValue [
    { value.args = [ "--foo" ]; }
    { value.args = [ "--bar" ]; }
  ];

  # A marked attrset with no plain list at the same path must still become a
  # real list. `types.oneOf` is a left fold of `either`, so an `attrsOf` branch
  # that accepts any attrset would take this definition first and keep the
  # `_type` marker in the output. The type guards against that.
  loneMkNamedListBecomesList = evalValue [
    { value.containers = lib.mkNamedList { main.image = "v1"; }; }
  ];

  loneMkNumberedListBecomesList = evalValue [
    {
      value.initContainers = lib.mkNumberedList {
        "1".image = "second";
        "0".image = "first";
      };
    }
  ];

  # An entry that only the marker introduces must still get its `name`. The key
  # is the only source of the name.
  nameInjectedForNewEntry = evalValue [
    {
      value.containers = [
        {
          name = "main";
          image = "base";
        }
      ];
    }
    { value.containers = lib.mkNamedList { sidecar.image = "s"; }; }
  ];

  # The key wins over a `name` inside the value.
  keyWinsOverInnerName = evalValue [
    {
      value.containers = [
        {
          name = "a";
          image = "A";
        }
      ];
    }
    {
      value.containers = lib.mkNamedList {
        b = {
          name = "DIFFERENT";
          image = "B";
        };
      };
    }
  ];

  # A patch of one entry must not reorder the list. The plain list definitions
  # give the order. An alphabetical sort would put "alpha" first.
  orderPreservedUnderNamedOverride = evalValue [
    {
      value.containers = [
        { name = "zeta"; }
        { name = "alpha"; }
      ];
    }
    { value.containers = lib.mkNamedList { zeta.image = "1"; }; }
  ];

  # A name that only the marker introduces goes after the plain list entries.
  newNamesAppendAfterPlainEntries = evalValue [
    {
      value.containers = [
        { name = "zeta"; }
        { name = "alpha"; }
      ];
    }
    { value.containers = lib.mkNamedList { beta.image = "b"; }; }
  ];

  mkMergeOfTwoMarkedLists = evalValue [
    {
      value.containers = lib.mkMerge [
        (lib.mkNamedList { a.image = "x"; })
        (lib.mkNamedList { b.image = "y"; })
      ];
    }
  ];

  mkMergeOfPlainListAndMarked = evalValue [
    {
      value.containers = lib.mkMerge [
        [
          {
            name = "a";
            image = "x";
          }
        ]
        (lib.mkNamedList { b.image = "y"; })
      ];
    }
  ];

  # `mkForce` on the whole marked definition drops the plain list. Only the
  # marked definition is left, and it must still become a real list.
  mkForceWholeMarkedList = evalValue [
    {
      value.containers = [
        {
          name = "a";
          image = "x";
        }
      ];
    }
    { value.containers = lib.mkForce (lib.mkNamedList { b.image = "y"; }); }
  ];

  mkIfFalseMarkedListIsDropped = evalValue [
    {
      value.containers = [
        {
          name = "a";
          image = "x";
        }
      ];
    }
    { value.containers = lib.mkIf false (lib.mkNamedList { a.image = "y"; }); }
  ];

  mkIfTrueMarkedListApplies = evalValue [
    {
      value.containers = [
        {
          name = "a";
          image = "x";
        }
      ];
    }
    { value.containers = lib.mkIf true (lib.mkNamedList { a.image = lib.mkForce "y"; }); }
  ];

  # `mkBefore` and `mkAfter` work on a plain list. Nothing here uses a marker.
  mkOrderOnPlainLists = evalValue [
    { value.containers = lib.mkAfter [ { name = "zzz"; } ]; }
    { value.containers = lib.mkBefore [ { name = "aaa"; } ]; }
    { value.containers = [ { name = "mmm"; } ]; }
  ];

  # An index override reaches a list of scalars, such as `args` or `command`.
  numberedOverrideOfScalarList = evalValue [
    {
      value.args = [
        "--a"
        "--b"
      ];
    }
    { value.args = lib.mkNumberedList { "1" = lib.mkForce "--B"; }; }
  ];

  # An index above the end of the list adds a new entry at the end.
  numberedSparseIndexAppends = evalValue [
    { value.containers = [ { name = "a"; } ]; }
    {
      value.containers = lib.mkNumberedList {
        "5" = {
          name = "f";
        };
      };
    }
  ];

  # A marked list inside a marked list entry.
  nestedNamedListOverride = evalValue [
    {
      value.containers = [
        {
          name = "a";
          env = [
            {
              name = "V";
              value = "1";
            }
          ];
        }
      ];
    }
    {
      value.containers = lib.mkNamedList {
        a.env = lib.mkNamedList { V.value = lib.mkForce "2"; };
      };
    }
  ];

  # A whole value that is only `null`. `types.nullOr` is still a legacy type,
  # so this type carries its own null branch instead. See kubeValueType.nix.
  topLevelNull = evalValue [ { value = null; } ];

  # One evaluation that keeps the option, so the test can read `valueMeta`.
  # The v2 merge protocol publishes it, and a consumer that walks the option
  # tree needs it to survive the recursion down to a list entry.
  metadataEvaluation = lib.evalModules {
    modules = [
      {
        options.value = lib.mkOption {
          type = kubeValueType;
          default = null;
        };
        config.value = {
          containers = [
            {
              name = "app";
              image = "v1";
            }
            {
              name = "sidecar";
              image = "s1";
            }
          ];
          args = [
            "--first"
            "--second"
          ];
          spec.enabled = true;
        };
      }
      {
        value = {
          containers = lib.mkNamedList { app.image = lib.mkForce "v2"; };
          args = lib.mkNumberedList { "1" = lib.mkForce "--changed"; };
        };
      }
    ];
  };

  # An object's fields go through `conditionalAttrsOf`, so `lib.mkIfExists`
  # works below the object as well as above it. `replicas` is there, so the
  # marker patches it. `paused` is not, so the marker creates nothing.
  conditionalFieldInsideObject = evalValue [
    { value.spec.replicas = 1; }
    {
      value.spec = lib.mkIfExists {
        replicas = lib.mkForce 3;
        paused = lib.mkIfExists true;
      };
    }
  ];

  # ---------------------------------------------------------------------
  # Priorities on a whole list definition.
  #
  # The module system resolves these before this type runs. `mergeDefinitions`
  # calls `filterOverrides` on the definitions and only then calls
  # `namedListOf.merge`, so a priority decides which definitions this type ever
  # sees. Nothing here is this type's own behaviour; that is the point. A
  # Kubernetes list must obey the same rules as any other option.
  # ---------------------------------------------------------------------

  # The plain case, which is what a reader reaches for first. Every existing
  # test forces a *marked* definition instead.
  mkForcePlainListOverPlain = evalValue [
    { value.args = [ "--a" ]; }
    { value.args = lib.mkForce [ "--b" ]; }
  ];

  mkDefaultPlainListLoses = evalValue [
    { value.args = lib.mkDefault [ "--fallback" ]; }
    { value.args = [ "--chosen" ]; }
  ];

  mkDefaultPlainListAloneApplies = evalValue [
    { value.args = lib.mkDefault [ "--fallback" ]; }
  ];

  # `mkForce []` is how a module empties a list. It has to survive as a real
  # empty list rather than becoming an absent field.
  mkForceEmptiesList = evalValue [
    {
      value.args = [
        "--a"
        "--b"
      ];
    }
    { value.args = lib.mkForce [ ]; }
  ];

  # Two definitions at the same priority concatenate. They do not conflict.
  # This surprises people: `mkForce` reads as "the last word", and it is only
  # "beats every lower priority". `listOf` then concatenates the survivors.
  twoMkForcesConcatenate = evalValue [
    { value.args = [ "--dropped" ]; }
    { value.args = lib.mkForce [ "--a" ]; }
    { value.args = lib.mkForce [ "--b" ]; }
  ];

  # An explicit numeric priority. Lower wins, as everywhere else.
  mkOverridePriorityOrder = evalValue [
    { value.args = lib.mkOverride 70 [ "--loser" ]; }
    { value.args = lib.mkOverride 60 [ "--winner" ]; }
  ];

  # The precedence rule that ties the whole file together: a priority on a
  # definition resolves BEFORE this type looks for a marker. `filterOverrides`
  # drops the ordinary `mkNamedList` definition, so `anyNamed` is false and the
  # plain list branch runs. The patch is discarded without a word.
  #
  # A module that forces a list therefore also silences every named patch of
  # it. That is `mkForce`'s meaning and not a defect, but it is invisible.
  mkForceDiscardsLaterNamedPatch = evalValue [
    {
      value.containers = [
        {
          name = "app";
          image = "v1";
        }
      ];
    }
    {
      value.containers = lib.mkForce [
        {
          name = "replacement";
          image = "v9";
        }
      ];
    }
    { value.containers = lib.mkNamedList { app.image = "never-applied"; }; }
  ];

  # ---------------------------------------------------------------------
  # The differential block: an unmarked list must behave exactly like
  # `types.listOf`.
  #
  # `types.raw` as the element type, because `raw.merge` is `mergeOneOption`
  # and `listOf` hands each element exactly one definition. The control is
  # therefore identity on plain data, while still running `dischargeProperties`
  # and `filterOverrides` for each element -- which is what makes the
  # element-level cases below meaningful.
  #
  # Each binding gives both types the same definitions. The test asserts the
  # two agree, so a divergence prints as a diff rather than as a bare
  # "expected X".
  # ---------------------------------------------------------------------
  evalBoth =
    definitions:
    let
      evaluate =
        type:
        (lib.evalModules {
          modules = [
            {
              options.value = lib.mkOption {
                inherit type;
                default = [ ];
              };
            }
          ]
          ++ map (definition: { value = definition; }) definitions;
        }).config.value;
    in
    {
      kube = evaluate kubeValueType;
      control = evaluate (lib.types.listOf lib.types.raw);
    };

  sameAsListOf = {
    concatenates = evalBoth [
      [ "--a" ]
      [ "--b" ]
    ];
    forced = evalBoth [
      [ "--a" ]
      (lib.mkForce [ "--b" ])
    ];
    defaulted = evalBoth [
      (lib.mkDefault [ "--fallback" ])
      [ "--chosen" ]
    ];
    ordered = evalBoth [
      (lib.mkAfter [ "--last" ])
      (lib.mkBefore [ "--first" ])
      [ "--middle" ]
      (lib.mkOrder 400 [ "--late" ])
    ];
    oneDefinitionSwitchedOff = evalBoth [
      [ "--kept" ]
      (lib.mkIf false [ "--dropped" ])
    ];
    everyDefinitionSwitchedOff = evalBoth [
      (lib.mkIf false [ "--dropped" ])
    ];
    emptyDefinition = evalBoth [
      [ ]
      [ "--a" ]
    ];
    # A property on one ELEMENT rather than on the definition. `listOf` merges
    # each element on its own, so `dischargeProperties` runs there too.
    elementSwitchedOff = evalBoth [
      [
        "--kept"
        (lib.mkIf false "--dropped")
      ]
    ];
    elementForced = evalBoth [
      [ (lib.mkForce "--forced") ]
    ];
  };

  # ---------------------------------------------------------------------
  # Priorities INSIDE an entry of a marked list.
  #
  # A marked list merges its entries through `types.attrsOf elemType`, so an
  # entry gets the full module merge that any other option value gets. These
  # pin that, because "the override mechanism reaches inside a list element" is
  # the whole reason the markers exist.
  # ---------------------------------------------------------------------

  # A field the plain list already sets beats a `mkDefault` in the patch.
  namedEntryMkDefaultLoses = evalValue [
    {
      value.containers = [
        {
          name = "app";
          image = "from-chart";
        }
      ];
    }
    { value.containers = lib.mkNamedList { app.image = lib.mkDefault "from-patch"; }; }
  ];

  # ...and a `mkDefault` for a field the plain list does NOT set still lands.
  namedEntryMkDefaultFillsAGap = evalValue [
    {
      value.containers = [
        {
          name = "app";
          image = "from-chart";
        }
      ];
    }
    { value.containers = lib.mkNamedList { app.imagePullPolicy = lib.mkDefault "IfNotPresent"; }; }
  ];

  # `mkMerge` inside an entry, the way it works anywhere else.
  namedEntryMkMerge = evalValue [
    { value.containers = [ { name = "app"; } ]; }
    {
      value.containers = lib.mkNamedList {
        app = lib.mkMerge [
          { image = "v1"; }
          { imagePullPolicy = "Always"; }
        ];
      };
    }
  ];

  # `mkIf false` on a whole entry that the plain list also defines. The patch
  # is discharged to nothing, and the plain definition is untouched -- so this
  # is a no-op rather than a deletion. A module cannot remove an entry by
  # switching its own patch off.
  namedEntrySwitchedOffLeavesThePlainEntry = evalValue [
    {
      value.containers = [
        {
          name = "app";
          image = "v1";
        }
      ];
    }
    { value.containers = lib.mkNamedList { app = lib.mkIf false { image = "v2"; }; }; }
  ];

  # `mkIf false` on an entry only the marker introduces. No definition survives
  # for that key, so `attrsOf` drops it and the entry never reaches the list.
  namedEntrySwitchedOffIsNotAdded = evalValue [
    {
      value.containers = [
        {
          name = "app";
          image = "v1";
        }
      ];
    }
    { value.containers = lib.mkNamedList { sidecar = lib.mkIf false { image = "s1"; }; }; }
  ];

  namedEntrySwitchedOnIsAdded = evalValue [
    {
      value.containers = [
        {
          name = "app";
          image = "v1";
        }
      ];
    }
    { value.containers = lib.mkNamedList { sidecar = lib.mkIf true { image = "s1"; }; }; }
  ];

  # The invariant that keeps `valueMeta` usable: one metadata entry per element
  # of the value, after every drop and append above has happened.
  metadataTracksTheValueEvaluation = lib.evalModules {
    modules = [
      {
        options.value = lib.mkOption {
          type = kubeValueType;
          default = null;
        };
        config.value.containers = [
          {
            name = "app";
            image = "v1";
          }
          {
            name = "sidecar";
            image = "s1";
          }
        ];
      }
      {
        value.containers = lib.mkNamedList {
          app.image = lib.mkForce "v2";
          dropped = lib.mkIf false { image = "d"; };
          added.image = "a";
        };
      }
    ];
  };

  numberedEntryMkDefaultLoses = evalValue [
    {
      value.initContainers = [
        {
          name = "migrate";
          image = "m1";
        }
      ];
    }
    { value.initContainers = lib.mkNumberedList { "0".image = lib.mkDefault "m2"; }; }
  ];

  # An index the plain list does not reach, switched off. Nothing is appended.
  numberedEntrySwitchedOffIsNotAdded = evalValue [
    { value.initContainers = [ { name = "migrate"; } ]; }
    { value.initContainers = lib.mkNumberedList { "3" = lib.mkIf false { name = "late"; }; }; }
  ];

  # ---------------------------------------------------------------------
  # Which branch of `oneOf` takes a definition.
  #
  # Everything above tests how definitions merge. This tests where they land
  # first, which is a separate mechanism and the one that decides whether a
  # value keeps its Nix type on the way to JSON. `types.oneOf` is a left fold
  # of `either`, and `either` takes the first branch whose `check` accepts
  # every definition -- so the order in `baseType` is load-bearing, and a
  # branch whose check is too wide silently swallows a value meant for a later
  # one.
  # ---------------------------------------------------------------------

  # Each scalar keeps its own Nix type. An int that arrives as a float, or a
  # bool that arrives as a string, is a manifest the API server rejects.
  scalarBranches = evalValue [
    {
      value = {
        anInt = 3;
        aFloat = 3.0;
        aString = "3";
        aTrue = true;
        aFalse = false;
      };
    }
  ];

  # `types.path` is a branch of its own, and nothing exercised it. A path
  # renders as its store path, not as an attribute set.
  pathBranch = evalValue [ { value.file = ./test_kube_value_type.nix; } ];

  # An empty attribute set is an object, and an empty list is a list. Both are
  # legal Kubernetes values, and `{}` and `[]` are not interchangeable there.
  # `namedListOf` comes before `objectType`, so an empty list has to fail
  # `objectType` rather than be taken by it.
  emptyObjectStaysAnObject = evalValue [ { value.spec = { }; } ];
  emptyListStaysAList = evalValue [ { value.args = [ ]; } ];

  # A marked attribute set nested inside an object, rather than at the option
  # root. `objectType`'s `addCheck` is what stops the object branch taking it:
  # its own check is only `isAttrs`, so without the guard the marker would
  # merge as an ordinary object and keep `_type` in the rendered manifest.
  markedListInsideAnObject = evalValue [
    { value.spec.template.containers = lib.mkNamedList { a.image = "x"; }; }
  ];

  # The following bindings must throw. Each one is exposed as a thunk, so the
  # test can assert on the error without an eager evaluation above.

  # Two definitions of one field inside an entry, at the same priority. This is
  # why every other test in this file writes `mkForce`: without one, a patch of
  # a field the plain list already sets is a conflict, not an override.
  namedEntryConflictThrows = evalValue [
    {
      value.containers = [
        {
          name = "app";
          image = "v1";
        }
      ];
    }
    { value.containers = lib.mkNamedList { app.image = "v2"; }; }
  ];

  numberedEntryConflictThrows = evalValue [
    {
      value.initContainers = [
        {
          name = "migrate";
          image = "m1";
        }
      ];
    }
    { value.initContainers = lib.mkNumberedList { "0".image = "m2"; }; }
  ];

  # No branch of `oneOf` accepts both an int and a string, so one field cannot
  # be defined as both. This is the same rejection `unmarkedAttrsRejectedAgainstList`
  # relies on, at the scalar level: a Kubernetes field has one fixed type.
  intAndStringThrows = evalValue [
    { value.replicas = 3; }
    { value.replicas = "3"; }
  ];

  # A function matches no branch at all. Without a rejection it would reach
  # `builtins.toJSON` and fail there instead, naming neither the option nor
  # the module that wrote it.
  aFunctionThrows = evalValue [ { value.callback = (x: x); } ];

  # A numbered list takes its order from its index keys, exactly as a named
  # list takes its order from its name keys. An order property on an entry
  # therefore does nothing, so it is refused rather than discharged in
  # silence. `mkOrderOnNamedEntryThrows` is the same rule on the other branch.
  mkOrderOnNumberedEntryThrows = evalValue [
    { value.initContainers = [ { name = "migrate"; } ]; }
    { value.initContainers = lib.mkNumberedList { "0" = lib.mkBefore { image = "m2"; }; }; }
  ];

  # `null` and a value cannot both be definitions of one field. This is
  # `types.nullOr`'s own rule; `oneOf` enforces it here, because no single
  # branch accepts both.
  mixedNullAndValueThrows = evalValue [
    { value = null; }
    { value.present = true; }
  ];

  # One field cannot use both markers. Earlier the named branch won in silence
  # and left a literal `true` in the list.
  mixedNamedAndNumberedThrows = evalValue [
    { value.containers = [ { name = "a"; } ]; }
    { value.containers = lib.mkNamedList { a.x = "1"; }; }
    { value.containers = lib.mkNumberedList { "0".y = "2"; }; }
  ];

  # A named list takes its order from the keys. `mkBefore` on an entry has no
  # effect, so the type refuses it instead of dropping it in silence.
  mkOrderOnNamedEntryThrows = evalValue [
    { value.containers = [ { name = "mid"; } ]; }
    { value.containers = lib.mkNamedList { early = lib.mkBefore { image = "e"; }; }; }
  ];

  mkNamedListRejectsNonAttrsInput = lib.mkNamedList [ { name = "a"; } ];
  mkNamedListRejectsNonAttrsValues = lib.mkNamedList { a = "not-an-attrset"; };
  mkNumberedListRejectsNonIntKeys = lib.mkNumberedList {
    notanumber = {
      image = "x";
    };
  };
in
{
  usesV2Merge = kubeValueType.merge ? v2 && kubeValueType.check.isV2MergeCoherent;
  exposesObjectMetadata = metadataEvaluation.options.value.valueMeta.attrs ? spec;
  exposesNamedListMetadata =
    lib.length metadataEvaluation.options.value.valueMeta.attrs.containers.list == 2;
  exposesNumberedListMetadata =
    lib.length metadataEvaluation.options.value.valueMeta.attrs.args.list == 2;
  inherit topLevelNull;
  inherit conditionalFieldInsideObject;
  inherit namedListOverrideViaMkNamedList;
  inherit plainListOfNamedThingsNeverAutoConverted;
  inherit ownerReferencesWithDuplicateNamesPreserved;
  inherit initContainersOverrideViaMkNumberedList;
  inherit plainListPassthrough;
  inherit nestedAttrsMergeAcrossModules;
  inherit scalarsPassthrough;
  inherit multiDefPlainListConcatenates;
  inherit loneMkNamedListBecomesList;
  inherit loneMkNumberedListBecomesList;
  inherit nameInjectedForNewEntry;
  inherit keyWinsOverInnerName;
  inherit orderPreservedUnderNamedOverride;
  inherit newNamesAppendAfterPlainEntries;
  inherit mkMergeOfTwoMarkedLists;
  inherit mkMergeOfPlainListAndMarked;
  inherit mkForceWholeMarkedList;
  inherit mkIfFalseMarkedListIsDropped;
  inherit mkIfTrueMarkedListApplies;
  inherit mkOrderOnPlainLists;
  inherit numberedOverrideOfScalarList;
  inherit numberedSparseIndexAppends;
  inherit nestedNamedListOverride;

  inherit
    mkForcePlainListOverPlain
    mkDefaultPlainListLoses
    mkDefaultPlainListAloneApplies
    mkForceEmptiesList
    twoMkForcesConcatenate
    mkOverridePriorityOrder
    mkForceDiscardsLaterNamedPatch
    sameAsListOf
    namedEntryMkDefaultLoses
    namedEntryMkDefaultFillsAGap
    namedEntryMkMerge
    namedEntrySwitchedOffLeavesThePlainEntry
    namedEntrySwitchedOffIsNotAdded
    namedEntrySwitchedOnIsAdded
    numberedEntryMkDefaultLoses
    numberedEntrySwitchedOffIsNotAdded
    scalarBranches
    emptyObjectStaysAnObject
    emptyListStaysAList
    markedListInsideAnObject
    ;

  # The store path the option resolved to, as a string the test can compare
  # against the file's own name.
  pathBranch = builtins.baseNameOf pathBranch.file;

  # One metadata entry per element of the value, after a force, a drop and an
  # append have all happened to the same list.
  metadataTracksTheValue = {
    value = metadataTracksTheValueEvaluation.config.value.containers;
    metaLength = lib.length metadataTracksTheValueEvaluation.options.value.valueMeta.attrs.containers.list;
  };

  # Forcing these must throw -- each is exposed as a thunk so the test can
  # assert on the error without eagerly evaluating it above.
  namedEntryConflictThrows = namedEntryConflictThrows;
  numberedEntryConflictThrows = numberedEntryConflictThrows;
  mkOrderOnNumberedEntryThrows = mkOrderOnNumberedEntryThrows;
  intAndStringThrows = intAndStringThrows;
  aFunctionThrows = aFunctionThrows;
  unmarkedAttrsRejectedAgainstListThrows = unmarkedAttrsRejectedAgainstListThrows;
  mixedNullAndValueThrows = mixedNullAndValueThrows;
  mixedNamedAndNumberedThrows = mixedNamedAndNumberedThrows;
  mkOrderOnNamedEntryThrows = mkOrderOnNamedEntryThrows;
  mkNamedListRejectsNonAttrsInput = mkNamedListRejectsNonAttrsInput;
  mkNamedListRejectsNonAttrsValues = mkNamedListRejectsNonAttrsValues;
  mkNumberedListRejectsNonIntKeys = mkNumberedListRejectsNonIntKeys;
}
