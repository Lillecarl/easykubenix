# `ekn _applyManifest` against a real Kubernetes cluster.
#
#     nix build --file ./checks.nix kubeapply
#
# The validation gate (../../easykubenix/validation.nix) applies the same
# manifests to a bare etcd and kube-apiserver, which answers whether the
# objects are well formed. It cannot answer anything else: there is no
# kubelet, no controller manager, and no apiextensions controller, so a CRD
# never becomes Established, a Deployment never gets a Pod, and a prune
# deletes objects nothing was ever going to act on.
#
# This boots a real single-node kubeadm cluster and applies into it. It runs
# under User-Mode Linux, so the guest is an ordinary process: no KVM, no root
# and no tap device, and the whole test is a derivation that passes or fails.
#
# `ekn` runs *inside* the guest. The guest sees the host's /nix/store over
# hostfs, so the program and the manifests are already there -- nothing is
# copied in, and nothing is fetched. `settings` carries their store paths,
# which is what makes the derivation depend on them.
#
# Deliberately outside `checks.all`. A control plane under UML is minutes of
# CPU, and CI builds `all` on every change; see ../default.nix.
{
  pkgs,
  lib,
  sources,
}:
let
  # `+ "/lib.nix"` and not string interpolation. Interpolating the source
  # would copy the whole checkout -- `.jj` included -- into the store.
  uml = import (sources.user-mode-nixos + "/lib.nix") { inherit pkgs; };

  # For `workloadImage`, which is the tag modules/k8s.nix imports into
  # containerd at boot. A pod may only use it with `imagePullPolicy: Never`.
  images = pkgs.callPackage (sources.user-mode-nixos + "/modules/k8s-images.nix") { };

  manifests = import ./manifests.nix { inherit (images) workloadImage; };

  # One easykubenix evaluation per module set. This repository's own
  # ../../default.nix, so the manifests come out of the same modules a user
  # gets, and `ekn` comes out of the same `ekn/`.
  evals = lib.mapAttrs (
    _name: module:
    import ../../default.nix {
      inherit pkgs sources;
      modules = [ module ];
    }
  ) manifests.modules;

  # `_applyManifest` is handed already-evaluated data rather than re-entering
  # Nix from a store path, so the barrier order arrives as a file. Every
  # generation here leaves `ekn.resourcePriority` at its default, so one file
  # answers for all of them.
  resourcePriorityFile = pkgs.writeText "resource-priority.json" (
    builtins.toJSON evals.gen1.config.ekn.resourcePriority
  );

  # A single control plane. `bring_up` unTaints a one-node cluster, because
  # there is nowhere else to put a Pod.
  #
  # More memory than the three-node test's control plane: this one also runs
  # the workload, and `ekn` itself.
  node = {
    imports = [ (sources.user-mode-nixos + "/modules/k8s.nix") ];
    services.uml-k8s = {
      enable = true;
      role = "control-plane";
    };
    boot.uml = {
      memory = "3072M";
      diskSize = 2048;
      lan = {
        network = "kubeapply";
        address = "10.102.0.1/24";
      };
    };
  };
in
uml.mkTest {
  name = "kubeapply";
  script = ./test.py;
  nodes.cp = node;
  settings = {
    ekn = lib.getExe' evals.gen1.passthru.ekn "ekn";
    resourcePriority = "${resourcePriorityFile}";
    manifests = lib.mapAttrs (_name: eval: "${eval.manifestJSONFile}") evals;

    inherit (manifests)
      namespace
      otherNamespace
      discriminator
      otherDiscriminator
      ;

    inherit (images) workloadImage;
    kubernetesVersion = pkgs.kubernetes.version;
  };
}
