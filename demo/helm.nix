# Helm example
{ helm, ... }:
{
  config = {
    kubernetes.resources.kube-system.Secret.hcloud.data.token = "";
    helm.releases.hccm = {
      namespace = "hccm-system";

      chart = helm.fetch {
        repo = "https://charts.hetzner.cloud";
        chart = "hcloud-cloud-controller-manager";
        version = "1.27.0";
        sha256 = "sha256-mzW5gQaRSPPKixaaJTTO+z7eQZcYhCRGulPnv9k/3Hg=";
      };

      values = {
        env.HCLOUD_INSTANCES_ADDRESS_FAMILY.value = "dualstack";
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
