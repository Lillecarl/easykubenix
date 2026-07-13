{
  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixpkgs-unstable";
    easykubenix.url = "path:../../../.";
  };
  outputs =
    {
      self,
      nixpkgs,
      easykubenix,
    }:
    let
      forEachSystem = nixpkgs.lib.genAttrs [ "x86_64-linux" ];
      eachPkgs = forEachSystem (system: import nixpkgs { inherit system; });
    in
    {
      eknConfig = forEachSystem (
        system:
        let
          pkgs = eachPkgs.${system};
        in
        {
          myapp =
            (easykubenix.lib.easykubenix {
              inherit pkgs;
              modules = [
                {
                  kubernetes.objects.default.ConfigMap.test = {
                    data.key = "hello-flake";
                  };
                  gitops.enable = true;
                  gitops.branch = "flake-branch";
                }
              ];
            }).config;
        }
      );
    };
}
