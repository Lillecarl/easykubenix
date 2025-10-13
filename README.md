# easykubenix

`easykubenix` uses the NixOS module system to generate Kubernetes manifests. It
avoids generating Nix types for the entire Kubernetes API, resulting in faster
evaluations and a simpler user experience compared to alternatives.

Manifest validation is performed by a script that applies the generated
resources against an ephemeral `etcd` and `kube-apiserver` instance. This
approach uses the Kubernetes API server as the single source of truth for
validation.

## Usage

Define your resources using the NixOS module system. The top-level attribute
is `kubernetes`, followed by the resource `kind`, then the resource name.

```nix
# my-manifests.nix
{
  kubernetes.ConfigMap.my-awesome-configmap = {
    metadata.namespace = "mynamespace123";
    data."config.json" = builtins.toJSON { key = "value"; };
  };

  kubernetes.Deployment.my-app = {
    metadata.name = "my-app";
    spec.replicas = 3;
    # ...
  };
}
```

To generate the final YAML manifests, import your modules into the provided
`eval` function.

```nix
# default.nix
{ pkgs ? import <nixpkgs> {} }:

(import <easykubenix> {
  inherit pkgs;
  modules = [ ./my-manifests.nix ];
}).eval
```

## Features

### Manifest Validation

To validate your manifests against a real Kubernetes API server without
affecting a live cluster, run the validation script.

```sh
nix run --file $youreasynix easykubenix.validator
```

This command builds your manifests, spins up a temporary API server, and
applies the configuration to it, reporting any errors from `kubectl`.

### Helm Chart Rendering

`easykubenix` can render Helm charts and import their resources into the NixOS
module system. This allows you to override values from rendered charts using
standard module system functions like `lib.mkForce`.

The import is performed via Import From Derivation (IFD), which is necessary
as it requires running `helm template` during Nix evaluation.

### Kluctl Integration

`kluctl` is a CLI and GitOps tool that deploys manifests. It's distinctive
feature is that it adds a label (discriminator) to every resource deployed which
means when applying manifests with --prune it scans the watch-cache for resources
with this label and removes them if they're not in the manifest we're applying.

Essentially kubectl apply --prune -l on steroids. It also integrates SOPS
which can be used to keep secrets out of the Nix store.

`easykubenix` supports generating a minimal kluctl project and deployment script.

