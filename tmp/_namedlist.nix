let
  pkgs = import <nixpkgs> { };
  lib = pkgs.lib.extend (import ../lib);

  input = {
    containers = {
      _namedlist = true;
      acontainer = {
        image = "bogus";
        args = [
          "this"
          "is"
          "args"
        ];
        env = {
          _namedlist = true;
          VAR.value = "VALUE";
        };
      };
    };
  };
in
rec {
  ainputVersion = input;
  blistVersion = (lib.walkWithPath lib.kubeAttrsToLists) ainputVersion;
  cattrVersion = (lib.walkWithPath lib.kubeListsToAttrs) blistVersion;
}
