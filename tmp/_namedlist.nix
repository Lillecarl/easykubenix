let
  pkgs = import <nixpkgs> { };
  # Corrected function
  transformNamedLists =
    { lib }:
    attrs:
    (lib.mapAttrsRecursiveCond (as: !(as ? "_namedlist")) (
      path: value:
      if value._namedlist or false == true then
        lib.pipe value [
          (lib.filterAttrs (n: _: n != "_namedlist"))
          lib.attrsToList
          (lib.map (v: v.value // { inherit (v) name; }))
        ]
      else
        value
    ) attrs);

  # Input with nested _namedlist
  input = {
    level1 = {
      systems = {
        _namedlist = true;
        x86_64-linux = {
          host = "server-a";
        };
        aarch64-linux = {
          host = "server-b";
        };
      };
      other = {
        foo = "bar";
      };
    };
  };
in
transformNamedLists { inherit (pkgs) lib; } input
