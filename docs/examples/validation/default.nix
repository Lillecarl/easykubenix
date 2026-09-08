{
  pkgs,
  lib,
  easykubenix,
}:
let
  ekn = easykubenix {
    inherit pkgs;
    modules = [
      {
        # Required, and no default. `validationScript` applies through the
        # same path as `ekn kubeapply`, which prunes by this label.
        ekn.discriminator = "validation-example";

        kubernetes.apiMappings = {
          Alertmanager = "monitoring.coreos.com/v1";
          Prometheus = "monitoring.coreos.com/v1";
          PrometheusRule = "monitoring.coreos.com/v1";
          ServiceMonitor = "monitoring.coreos.com/v1";
        };

        helm.releases."prometheus-stack" = {
          chart = pkgs.fetchHelm {
            repo = "https://prometheus-community.github.io/helm-charts";
            chart = "kube-prometheus-stack";
            version = "62.0.0";
            sha256 = "sha256-v8jOlnRMj23of+RqXjCKue8oUh8wprcZoip92BdMTAo=";
          };
          namespace = "monitoring";
          includeCRDs = true;
          noHooks = true;
          values = {
            defaultRules.enabled = false;
            alertmanager.enabled = false;
            grafana.enabled = false;
            kubeApiServer.enabled = false;
            kubelet.enabled = false;
            kubeControllerManager.enabled = false;
            kubeDns.enabled = false;
            kubeEtcd.enabled = false;
            kubeScheduler.enabled = false;
            kubeStateMetrics.enabled = false;
            kubeProxy.enabled = false;
            coreDns.enabled = false;
            nodeExporter.enabled = false;
            prometheusOperator.enabled = true;
            prometheus.enabled = true;
            prometheus.prometheusSpec.replicas = 1;
            prometheus.prometheusSpec.resources = { };
          };
        };

        kubernetes.objects.monitoring.PrometheusRule.test-alerts = {
          spec.groups = [
            {
              name = "test";
              rules = [
                {
                  alert = "TestAlert";
                  expr = "vector(1)";
                }
              ];
            }
          ];
        };
      }
    ];
  };
in
{
  inherit (ekn) manifestJSON manifestYAMLFile validationScript;
  check = import ../verify.nix {
    inherit pkgs lib;
    name = "validation-crds";
    manifestJSON = ekn.manifestJSON;
  };
}
