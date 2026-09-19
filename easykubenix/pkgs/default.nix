_: pkgs: {
  lib = pkgs.lib.extend (import ../lib);
  writeMultipleFiles = pkgs.callPackage ./writeMultipleFiles.nix { };
  fetchHelm = pkgs.callPackage ./fetchHelm.nix { };
  chart2yaml = pkgs.callPackage ./chart2yaml.nix { };
  renderChart = pkgs.callPackage ./renderChart.nix { };
  ekn-yaml2json = pkgs.callPackage ../../tools/yaml2json/package.nix { };
}
