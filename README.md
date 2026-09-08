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
`deployment.units.<name>.discriminator` for a `--target` apply, so pruning one
unit can never reach another's objects.

`ekn` stamps that label at apply time, so only the objects `ekn` itself applies
carry it. On a GitOps cluster that is the minority: nearly everything reaches
the API server through ArgoCD, which applies the committed YAML.

So a unit records itself in the manifest instead. Every object in
`deployment.units.<name>` renders with an `ekn.dev/deployment-unit` label
holding `<name>`, whatever ends up applying it:

```console
$ kubectl get all -A -l ekn.dev/deployment-unit=apps
```

It is a `mkDefault` entry in that unit's `labels`, so a unit can override the
value, or decline it with `lib.mkForce (_: null)` and record itself some other
way. Because the name becomes a label value, easykubenix asserts that it can be
one: at most 63 characters, starting and ending alphanumeric.

`kubernetes.generated` carries no such label. It is not unit-scoped, the same
as for every other per-unit label, so a whole-`generated` apply and a `--target`
apply of one object differ.

Objects apply in barriers ordered by `ekn.resourcePriority`, which defaults to
Helm's `InstallOrder` numbered in tens (`10`–`380`): namespaces and CRDs go down
before the things that need them, and CRDs are waited on until Established. The
steps of ten leave room to slot a kind between two neighbours without
renumbering the rest.

A kind not in that list — every custom resource — applies at 1000, leaving
`381`–`999` for "late, but before custom resources" and `1001`+ for "after
them". `APIService` and the two admission webhook configurations are numbered
into that last band, the one place this deviates from Helm: Helm sorts unknown
kinds after its whole list, which leaves all three ahead of every custom
resource they intercept. During a bootstrap their backing workload was applied
seconds earlier and is not serving yet, so each intercepted request costs a full
timeout — or, for an aggregated `APIService`, fails discovery outright.

### Bootstrap units

A deployment unit can carry its own module list, evaluated as an entirely
separate easykubenix configuration:

```nix
deployment.units.bootstrap = {
  path = "bootstrap";
  modules = [ ./bootstrap/argocd.nix ];
};
```

Its objects render into that unit's path and stay out of
`kubernetes.generated`, so a plain `ekn kubeapply` and `ekn validate` never see
them — only `ekn kubeapply --target bootstrap` applies them.

That is what makes it usable for the chicken-and-egg part of GitOps: the engine
itself, its credentials and its root Application cannot be synced by the engine,
because they are what lets it sync anything. They go down once, by hand, and
have a different lifecycle from everything else afterwards.

The nested instance is a complete configuration, not a cut-down one, so it can
render a Helm chart like any other. It gets its parent's evaluated config as the
`parent` module argument — a root Application has to name the branch it syncs —
and its `ekn.discriminator` defaults to the unit's, so the prune scope agrees
with what the apply uses. Read the parent's *inputs* through `parent`
(`deployment.deployBranch`, `ekn.discriminator`); reading its rendered outputs closes
a loop back through the nested instance and recurses.

### Handing a bootstrap unit over

Bootstrapping is only half the job: the same objects usually have to become
ordinary managed resources afterwards. Two knobs on the unit arrange that, and
they are deliberately separate mechanisms.

```nix
deployment.units.bootstrap = {
  path = "bootstrap";
  modules = [ ./bootstrap/argocd.nix ];

  # who owns each *field*
  fieldManager = "argocd-controller";

  # who owns the *object*
  annotations."argocd.argoproj.io/tracking-id" = ekn.lib.argocdTrackingId {
    app = "argocd";
    namespace = "argocd";
  };
};
```

`fieldManager` is apply-time only and never appears in the rendered manifests.
Applying as the successor rather than as `ekn` is what completes the handover:
server-side apply only drops a field when its *owning* manager stops declaring
it, and a bootstrap apply never runs again — so as a distinct manager, `ekn`
keeps owning every field the successor does not declare, permanently. It also
removes the need for `Force=true`. The cost is that a second apply of the same
unit silently overwrites the successor's fields instead of reporting a
conflict, which is acceptable only because a bootstrap unit runs once.

`labels` and `annotations` go the other way: they are baked into the rendered
manifests, so the committed YAML and the applied object agree. A value may be a
function of the object rather than a string, for metadata that has to encode the
object's own identity — ArgoCD's `tracking-id` is `<app>:<group>/<kind>:<ns>/<name>`,
so a constant would be wrong on all but one object, and wrong here fails
*silently*: ArgoCD reads a non-self-referencing id as naming something else and
then never prunes the object. `ekn.lib.argocdTrackingId` builds it, and returns
`null` — meaning "no entry" — for CRDs, which ArgoCD deliberately never stamps.

Target metadata wins over what the object already carries. Helm charts routinely
set `app.kubernetes.io/instance` to their release name, which is exactly the key
a GitOps engine may be reading to decide ownership.

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
