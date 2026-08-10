{
  config,
  pkgs,
  lib,
  ekn,
  eknPackage,
  ...
}:
let
  cfg = config.validation;
  debugpipe = if (!cfg.debug) then "&>/dev/null" else "";

  # The apply inputs, written out as files because `ekn _applyManifest` is
  # handed already-evaluated data rather than re-entering Nix from inside a
  # store-path script -- see its docstring in ekn/src/ekn/cli.py.
  resourcePriorityFile = pkgs.writeText "resource-priority.json" (
    builtins.toJSON config.ekn.resourcePriority
  );
  novalidateKeysFile = pkgs.writeText "novalidate-keys.json" (
    builtins.toJSON config.kubernetes.novalidateKeys
  );
in
{
  options.validation = {
    debug = lib.mkEnableOption "validation debugging";
    etcdPackage = lib.mkPackageOption pkgs "etcd" { };
    kubeconformPackage = lib.mkPackageOption pkgs "kubeconform" { };
    kubeadmConfig = lib.mkOption {
      type = ekn.lib.kubeValueType;
    };
    podSubnet = lib.mkOption {
      type = lib.types.str;
      default = "10.97.0.0/16";
    };
    serviceSubnet = lib.mkOption {
      type = lib.types.str;
      # Comma-separated IPv4+IPv6 CIDRs -- kube-apiserver only allows
      # RequireDualStack Services when --service-cluster-ip-range lists both
      # families, so a single-family default here would fail validation for
      # any manifest containing a dual-stack Service (e.g. via a Kyverno
      # mutate policy that sets ipFamilyPolicy=RequireDualStack cluster-wide)
      # even though the real target cluster supports it fine.
      default = "10.96.0.0/16,fd00:96::/112";
    };
    script = lib.mkOption {
      type = lib.types.package;
      internal = true;
    };
  };
  config.validation =
    let
      cfgFile = pkgs.writeText "ClusterConfiguration.json" (builtins.toJSON cfg.kubeadmConfig);
    in
    {
      # Set kubeadmConfig sane defaults
      kubeadmConfig = lib.mapAttrsRecursive (n: v: lib.mkDefault v) {
        apiVersion = "kubeadm.k8s.io/v1beta4";
        kind = "ClusterConfiguration";
        kubernetesVersion = "v${config.kubernetes.package.version}";
        controlPlaneEndpoint = "$BIND_ADDRESS:$KUBERNETES_PORT";
        certificatesDir = "$CERT_DIR";
        networking = {
          podSubnet = cfg.podSubnet;
          serviceSubnet = cfg.serviceSubnet;
        };
      };

      script =
        pkgs.writeScriptBin "kubeval" # fish
          ''
            #! ${lib.getExe pkgs.fishMinimal}
            set --prepend PATH ${cfg.etcdPackage}/bin
            set --prepend PATH ${config.kubernetes.package}/bin
            set --prepend PATH ${pkgs.retry}/bin
            set TMPDIR (mktemp --directory --suffix=eknvalidation)
            ${if cfg.debug then "echo TMPDIR: $TMPDIR" else ""}
            set --export CERT_DIR $TMPDIR/pki
            set KUBEADM_CONFIG $TMPDIR/kubeadm-config.json
            set --export KUBECONFIG $TMPDIR/admin.conf

            function cleanup --on-event fish_exit
              kill -9 $ETCD_PID 2>/dev/null || true
              kill -9 $APISERVER_PID 2>/dev/null || true
              ${if cfg.debug then "" else "rm -rf $TMPDIR"}
            end

            function get_free_port
                ${lib.getExe pkgs.python3Minimal} -c 'import socket; s=socket.socket(); s.bind(("", 0)); print(s.getsockname()[1]); s.close()'
            end

            set --export BIND_ADDRESS 127.0.0.1
            set --export KUBERNETES_PORT $(get_free_port)
            set ETCD_CLIENT_PORT $(get_free_port)
            set ETCD_PEER_PORT $(get_free_port)

            ${lib.getExe pkgs.envsubst} -i ${cfgFile} > $KUBEADM_CONFIG

            echo initializing with kubeadm
            kubeadm init phase certs all --config=$KUBEADM_CONFIG ${debugpipe} || exit 1
            kubeadm init phase kubeconfig admin --config=$KUBEADM_CONFIG --kubeconfig-dir=$TMPDIR ${debugpipe} || exit 1

            echo "kubeadm initialized; starting etcd"

            set command etcd --data-dir=$TMPDIR/etcd-data \
              --name=default \
              --listen-client-urls=https://127.0.0.1:$ETCD_CLIENT_PORT \
              --advertise-client-urls=https://127.0.0.1:$ETCD_CLIENT_PORT \
              --listen-peer-urls=https://127.0.0.1:$ETCD_PEER_PORT \
              --initial-advertise-peer-urls=https://127.0.0.1:$ETCD_PEER_PORT \
              --initial-cluster=default=https://127.0.0.1:$ETCD_PEER_PORT \
              --client-cert-auth=true \
              --trusted-ca-file=$CERT_DIR/etcd/ca.crt \
              --cert-file=$CERT_DIR/etcd/server.crt \
              --key-file=$CERT_DIR/etcd/server.key \
              --peer-client-cert-auth=true \
              --peer-trusted-ca-file=$CERT_DIR/etcd/ca.crt \
              --peer-cert-file=$CERT_DIR/etcd/peer.crt \
              --peer-key-file=$CERT_DIR/etcd/peer.key \
              --log-level=error
            ${if cfg.debug then "echo $command" else ""}
            $command ${debugpipe} &
            set ETCD_PID $last_pid

            retry \
              --times=10 \
              --delay=0 \
              --jitter=1 \
              -- \
                etcdctl \
                  --endpoints=https://127.0.0.1:$ETCD_CLIENT_PORT \
                  --cacert=$CERT_DIR/etcd/ca.crt \
                  --cert=$CERT_DIR/etcd/healthcheck-client.crt \
                  --key=$CERT_DIR/etcd/healthcheck-client.key \
                  endpoint health ${debugpipe} || exit 1

            echo "etcd is ready; starting kube-apiserver"

            set command kube-apiserver \
              --watch-cache=false \
              --anonymous-auth=false \
              --etcd-cafile=$CERT_DIR/etcd/ca.crt \
              --etcd-certfile=$CERT_DIR/apiserver-etcd-client.crt \
              --etcd-keyfile=$CERT_DIR/apiserver-etcd-client.key \
              --etcd-servers=https://127.0.0.1:$ETCD_CLIENT_PORT \
              --service-cluster-ip-range=10.96.0.0/12 \
              --bind-address=$BIND_ADDRESS \
              --secure-port=$KUBERNETES_PORT \
              --allow-privileged=true \
              --client-ca-file=$CERT_DIR/ca.crt \
              --kubelet-client-certificate=$CERT_DIR/apiserver-kubelet-client.crt \
              --kubelet-client-key=$CERT_DIR/apiserver-kubelet-client.key \
              --service-account-issuer=https://kubernetes.default.svc.cluster.local \
              --service-account-key-file=$CERT_DIR/sa.pub \
              --service-account-signing-key-file=$CERT_DIR/sa.key \
              --tls-cert-file=$CERT_DIR/apiserver.crt \
              --tls-private-key-file=$CERT_DIR/apiserver.key
            ${if cfg.debug then "echo $command" else ""}
            $command ${debugpipe} &
            set APISERVER_PID $last_pid

            retry \
              --times=10 \
              --delay=0 \
              --jitter=1 \
              -- \
                kubectl \
                  get \
                  --raw \
                  /healthz >/dev/null ${debugpipe} || exit 1

            echo "kube-apiserver ready; applying manifest(s)"

            # Applies through the same `apply_and_prune` as `ekn kubeapply`
            # (the bootstrap path) and `ekn validate`, rather than shelling
            # out to `kluctl deploy` as this used to. A conformance gate that
            # proves a deploy path nothing else uses proves the wrong thing:
            # barrier ordering, CRD-establish waits and SOPS handling are all
            # ekn's, and they are what runs against a real cluster.
            ${lib.getExe' eknPackage "ekn"} _applyManifest ${config.internal.manifestJSONFile} \
              --discriminator ${config.ekn.discriminator} \
              --resource-priority-file ${resourcePriorityFile} \
              --novalidate-keys-file ${novalidateKeysFile} || begin
              echo "ekn apply failed"
              exit 1
            end
            echo "dumping openapiv2 schema from apiserver"
            kubectl get --raw /openapi/v2 > $TMPDIR/k8s-schema.json
            echo "running kubeconform"
            ${lib.getExe cfg.kubeconformPackage} -schema-location $TMPDIR/k8s-schema.json -summary < ${config.internal.manifestJSONFile} || begin
              echo "kubeconform verification failed"
              exit 1
            end
            echo "Your manifests are as valid as they can be against Kubernetes ${config.kubernetes.package.version}"
          '';
    };
}
