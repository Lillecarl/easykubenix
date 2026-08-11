# easykubenix

[![Documentation](https://img.shields.io/badge/docs-lillecarl.github.io-blue)](https://lillecarl.github.io/easykubenix/)

Note that a lot of this text is AI slop(because i write like a toddler), don't
judge the book by it's cover though!

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

#### Quirks:
The namespace "none" in ```kubernetes.resources.none.kind.name``` is reserved
for not setting any namespace. If you create resources in the none namespace
you must set metadata.namespace yourself.

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

### Applying and pruning

`ekn kubeapply` server-side-applies the generated objects and can prune what a
previous apply left behind. It stamps every object it applies with an
`ekn.dev/discriminator` label, then lists objects back by that label and deletes
the ones the current apply no longer produces — `kubectl apply --prune -l` with
the ordering and SOPS handling filled in.

The label value comes from `ekn.discriminator`, or from
`gitOps.targets.<name>.discriminator` for a `--target` apply, so pruning one
target can never reach another's objects.

Objects apply in barriers ordered by `ekn.resourcePriority`, which defaults to
Helm's `InstallOrder`: namespaces and CRDs go down before the things that need
them, and CRDs are waited on until Established.

A kind not in that list — every custom resource — applies near the end, but
deliberately *ahead* of the admission webhook configurations rather than after
them. Helm sorts unknown kinds after its whole list, which puts every custom
resource behind the webhooks that intercept it; during a bootstrap the
webhook's backend was applied seconds earlier and is not serving yet, so each
intercepted write costs the webhook's full timeout.

### Kluctl integration (deprecated)

`kluctl` is a CLI and GitOps tool that deploys manifests, and easykubenix can
still generate a minimal kluctl project and deployment script. It predates
`ekn kubeapply`, which now covers the same ground natively. See
[issue #2](https://github.com/Lillecarl/easykubenix/issues/2); `kluctl.*`
options still work, and `kluctl.discriminator`/`kluctl.resourcePriority` have
moved to `ekn.*` with warnings pointing at the new paths.

[Documentation](https://lillecarl.github.io/easykubenix/)

---

*This project is made possible by*

[![Dynamist](.assets/dynamist-logo.png)](https://dynamist.se/)
