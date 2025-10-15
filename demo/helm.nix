# Helm example
{ pkgs, ... }:
{
  config = {
    # Since we're only setting one final subattribute this is really the entire
    # hetzner secret
    kubernetes.resources.kube-system.Secret.hcloud.stringData.token = "";
    helm.releases.hccm = {
      namespace = "kube-system";

      chart = pkgs.fetchHelm {
        repo = "https://charts.hetzner.cloud";
        chart = "hcloud-cloud-controller-manager";
        version = "1.27.0";
        sha256 = "sha256-mzW5gQaRSPPKixaaJTTO+z7eQZcYhCRGulPnv9k/3Hg=";
      };

      values = {
        additionalTolerations = [
          {
            key = "node.cilium.io/agent-not-ready";
            operator = "Exists";
          }
        ];
      };
    };
  };
}
