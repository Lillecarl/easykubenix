{
  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    easykubenix.url = "path:../";
  };
  outputs =
    inputs:
    let
      eachSys =
        func:
        inputs.nixpkgs.lib.genAttrs [
          "x86_64-linux"
          "aarch64-linux"
        ] func;
    in
    {
      packages = eachSys (
        system:
        let
          pkgs = import inputs.nixpkgs { inherit system; };
          inherit (pkgs) lib;

          hello-bash = pkgs.writeShellApplication {
            name = "hello-bash";
            text = "echo Hello, world!";
          };

          eknEval = inputs.easykubenix.lib.easykubenix {
            inherit pkgs;
            modules = [
              {
                kubernetes.objects.namespace.ConfigMap.name = {
                  data = "I am a configMap";
                };
                kubernetes.objects.namespace.Pod.hello = {
                  spec.containers = [
                    {
                      name = "hello";
                      image = "gcr.io/distroless/static:latest";
                      command = lib.getExe hello-bash;
                      volumeMounts = [
                        {
                          name = "nix-store";
                          mountPath = "/nix";
                          subPath = "nix";
                        }
                      ];
                    }
                  ];
                  spec.volumes = [
                    {
                      name = "nix-store";
                      csi = {
                        driver = "nix.csi.store";
                        readOnly = true;
                      };
                    }
                  ];
                };
              }
            ];
          };
        in
        {
          inherit (eknEval) manifestYAMLFile;
        }
      );
    };
}
