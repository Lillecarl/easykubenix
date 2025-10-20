let
  pkgs = import <nixpkgs> { };
  lib = pkgs.lib;

  # Helper function to recursively walk a data structure
  walk =
    f: v:
    if lib.isAttrs v then
      f (lib.mapAttrs (n: walk f) v)
    else if lib.isList v then
      f (map (walk f) v)
    else
      f v;

  walkExcludeAttrNames =
    attrNames: f: v:
    if lib.isAttrs v then
      # Apply transform to the attrset itself, then map over its children
      f (
        lib.mapAttrs (
          name: val:
          # If the key is initContainers, just recurse without the transform
          if lib.elem name attrNames then
            val
          # Otherwise, recurse normally
          else
            walkExcludeAttrNames attrNames f val
        ) v
      )
    else if lib.isList v then
      f (map (walkExcludeAttrNames attrNames f) v)
    else
      f v;

  listToNamedList =
    value:
    if lib.isList value && (lib.all (lib.hasAttr "name") value) then
      lib.pipe value [
        (lib.map (x: {
          inherit (x) name;
          value = lib.removeAttrs x [ "name" ];
        }))
        lib.listToAttrs
        (x: x // { _namedlist = true; })
      ]
    else
      value;

  namedListToList =
    value:
    if value._namedlist or false == true then
      lib.mapAttrsToList (
        name: val:
        if !lib.isAttrs val then
          throw "namedListToList error: Value for key '${name}' is not an attribute set."
        else
          val // { inherit name; }
      ) (lib.removeAttrs value [ "_namedlist" ])
    else
      value;

  input = {
    level1 = {
      initContainers = {
        _namedlist = true;
        acontainer = {
          image = "bogus";
          env = {
            _namedlist = true;
            VAR.value = "VALUE";
          };
        };
        bcontainer = {
          image = "bogus";
        };
      };
      containers = {
        _namedlist = true;
        acontainer = {
          image = "bogus";
          env = {
            _namedlist = true;
            VAR.value = "VALUE";
          };
        };
        bcontainer = {
          image = "bogus";
        };
      };
      other = {
        foo = "bar";
      };
    };
  };
in
rec {
  ainputVersion = input;
  blistVersion = (walk namedListToList) ainputVersion;
  cattrVersion = (walkExcludeAttrNames [ "initContainers" ] listToNamedList) blistVersion;
  dlistVersion = (walk namedListToList) cattrVersion;
  eattrVersion = (walkExcludeAttrNames [ "initContainers" ] listToNamedList) dlistVersion;
}
