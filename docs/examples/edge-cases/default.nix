{
  pkgs,
  lib,
  easykubenix,
}:
let
  inherit (pkgs) lib;

  ekn = easykubenix {
    inherit pkgs;
    modules = [
      {
        # Cluster-scoped resource (no namespace)
        kubernetes.objects.none.Namespace.custom-ns = { };

        # Resource in the namespace we just created
        kubernetes.objects.custom-ns.ConfigMap.ns-config = {
          data.ns = "custom-ns";
        };

        # Custom apiVersion via apiMappings
        kubernetes.apiMappings = {
          Certificate = "cert-manager.io/v1";
          ClusterIssuer = "cert-manager.io/v1";
        };

        kubernetes.objects.none.ClusterIssuer.selfsigned = {
          spec.selfSigned = { };
        };

        kubernetes.objects.none.Certificate.example = {
          spec.dnsNames = [ "example.com" ];
          spec.secretName = "example-tls";
          spec.issuerRef.name = "selfsigned";
          spec.issuerRef.kind = "ClusterIssuer";
        };

        # Nested namespace
        kubernetes.objects.kube-system.ConfigMap.kube-proxy-config = {
          data."config.conf" = "apiVersion: kubeproxy.config.k8s.io/v1alpha1\nkind: KubeProxyConfiguration\n";
        };
      }
    ];
  };
in
{
  manifestJSON = ekn.manifestJSON;
  check = import ../verify.nix {
    inherit pkgs lib;
    name = "edge-cases";
    manifestJSON = ekn.manifestJSON;
  };
}
