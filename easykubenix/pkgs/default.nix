final: pkgs: {
  lib = pkgs.lib.extend (import ../lib);
  writeMultipleFiles = pkgs.callPackage ./writeMultipleFiles.nix { };
  fetchHelm = pkgs.callPackage ./fetchHelm.nix { };
  chart2yaml = pkgs.callPackage ./chart2yaml.nix { };
  # `final` for the one argument this overlay itself defines. `callPackage` on
  # `pkgs` -- the prior set -- cannot see a sibling of this attribute.
  renderChart = pkgs.callPackage ./renderChart.nix { inherit (final) ekn-yaml2json; };
  ekn-yaml2json = pkgs.callPackage ../../tools/yaml2json/package.nix { };
}
