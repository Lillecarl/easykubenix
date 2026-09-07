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

  # The following bindings must throw. Each one is exposed as a thunk, so the
  # test can assert on the error without an eager evaluation above.

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
  # Forcing these must throw -- each is exposed as a thunk so the test can
  # assert on the error without eagerly evaluating it above.
  unmarkedAttrsRejectedAgainstListThrows = unmarkedAttrsRejectedAgainstListThrows;
  mixedNamedAndNumberedThrows = mixedNamedAndNumberedThrows;
  mkOrderOnNamedEntryThrows = mkOrderOnNamedEntryThrows;
  mkNamedListRejectsNonAttrsInput = mkNamedListRejectsNonAttrsInput;
  mkNamedListRejectsNonAttrsValues = mkNamedListRejectsNonAttrsValues;
  mkNumberedListRejectsNonIntKeys = mkNumberedListRejectsNonIntKeys;
}
