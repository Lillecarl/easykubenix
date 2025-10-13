# easykubenix
Note that a lot of this text is AI slop, don't judge the book by it's cover though!

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

### Try the demo
Evaluate the demo YAML and apply it to an ephemeral apiserver
```bash
nix run --file . validationScript
```
Check the generated YAML
```bash
cat $(nix build --print-out-paths --file . manifestYAMLFile)
```

### Modules API
```nix
{
  kubernetes.namespace.ConfigMap.my-awesome-configmap = {
    stringData."config.json" = builtins.toJSON { key = "value"; };
  };

  kubernetes.namespace.Deployment.my-app = {
    spec.replicas = 3;
  };
}
```
How to create an easykubenix instance (probably)
```nix
{ pkgs ? import <nixpkgs> {}}:
let
  easykubenix = import (
    builtins.fetchTree {
      type = "github";
      owner = "lillecarl";
      repo = "easykubenix";
    }
  );
in
easykubenix {
  inherit pkgs;
  modules = [
    ./my-modules.nix
  ];
}
```

To generate the final YAML manifests, import your modules into the provided
`eval` function.

```nix
# default.nix
{ pkgs ? import <nixpkgs> {} }:
(import <easykubenix> {
  inherit pkgs;
  modules = [ ./my-modules.nix ];
}).eval
```

## Features

### Manifest Validation

To validate your manifests against a real Kubernetes API server without
affecting a live cluster, run the validation script.

```bash
nix run --file . validationScript
```

This command builds your manifests, spins up a temporary API server, and
applies the configuration to it, reporting any errors from `kubectl`.

### Helm Chart Rendering

`easykubenix` can render Helm charts and import their resources into the NixOS
module system. This allows you to override values from rendered charts using
standard module system functions like `lib.mkForce`.

The import is performed via Import From Derivation (IFD), which is necessary
as it requires running `helm template` during Nix evaluation.

See the demo for examples

### Kluctl Integration

`kluctl` is a CLI and GitOps tool that deploys manifests. It's distinctive
feature is that it adds a label (discriminator) to every resource deployed which
means when applying manifests with --prune it scans the watch-cache for resources
with this label and removes them if they're not in the manifest we're applying.

Essentially kubectl apply --prune -l on steroids. It also integrates SOPS
which can be used to keep secrets out of the Nix store.

`easykubenix` supports generating a minimal kluctl project and deployment script.
