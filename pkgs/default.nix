_: pkgs: {
  lib = pkgs.lib.extend (import ../lib);
  writeMultipleFiles = pkgs.callPackage ./writeMultipleFiles.nix { };
  fetchHelm = pkgs.callPackage ./fetchHelm.nix { };
  chart2json = pkgs.callPackage ./chart2json.nix { };
}
