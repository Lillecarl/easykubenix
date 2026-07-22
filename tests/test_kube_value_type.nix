let
  compat = import ../nix/compat.nix;
  pkgs = import compat.inputs.nixpkgs { };
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
  # Forcing this one must throw -- exposed as a thunk so the test can
  # assert on the error without eagerly evaluating it above.
  unmarkedAttrsRejectedAgainstListThrows = unmarkedAttrsRejectedAgainstListThrows;
}
