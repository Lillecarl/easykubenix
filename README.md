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
`ekn.dev/environment` label, then lists objects back by label and deletes
the ones the current apply no longer produces — `kubectl apply --prune -l` with
the ordering and SOPS handling filled in.

Two labels decide the scope, not one:

| apply | prune selector |
| --- | --- |
| `ekn kubeapply --prune` | `ekn.dev/environment=E,!ekn.dev/deployment-unit` |
| `ekn kubeapply --target X --prune` | `ekn.dev/environment=E,ekn.dev/deployment-unit=X` |

`E` is `ekn.environment`. The not-exists clause is what keeps the two apart. A
deployment unit's objects never reach `kubernetes.generated` — a bootstrap unit
renders a whole nested instance, and only `--target <name>` applies it — so
without that clause a whole-instance prune would find them, see them absent
from its own desired set, and delete them.

`ekn` stamps the environment label at apply time, so only the objects `ekn`
itself applies carry it. On a GitOps cluster that is the minority: nearly
everything reaches the API server through ArgoCD, which applies the committed
YAML. That is deliberate — it is what stops `ekn` and the engine pruning each
other's work.

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
and its `ekn.environment` defaults to the parent's, because both applies stamp
that label and the unit label is what separates the scopes. Read the parent's
*inputs* through `parent` (`deployment.deployBranch`, `ekn.environment`);
reading its rendered outputs closes a loop back through the nested instance and
recurses.

### Unit dependencies

A unit can name other units it needs on the cluster:

```nix
deployment.units.bootstrap.dependencies = [ "bootstrap-secrets" ];
```

`ekn kubeapply --target bootstrap` then applies `bootstrap-secrets`' objects
too, transitively, each unit as its own group with its own `fieldManager`. The
point is the other direction: re-seeding the credentials is
`ekn kubeapply --target bootstrap-secrets` on its own, rather than a full
bootstrap apply — which is what you do only after breaking a cluster badly
enough that ArgoCD or the CNI is gone.

`--prune` still covers only the unit you named. Each unit keeps its own prune
scope, because `ekn.dev/deployment-unit` is rendered per unit, so bringing a
dependency along never widens what gets deleted.

A dependency cycle is an error, and so is a name no `deployment.units` entry
declares. `ekn commit` ignores dependencies entirely: they are about what has
to be on the cluster, not about where a manifest lives.

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

### OpenTofu units

A deployment unit can hold OpenTofu configuration instead of Kubernetes
objects:

```nix
deployment.units.infra = {
  class = "tf";
  path = "infra";
  modules = [ ./infra/cluster.nix ];
};
```

Its modules declare `tofu.{terraform,provider,resource,data,output,...}` rather
than `kubernetes.*`, and render to one `config.tf.json`. The module system
enforces the split: a nested `tf` instance is evaluated with `class = "tf"`, so
a Kubernetes module imported into it fails at the import naming both classes,
rather than failing once per option.

Providers are pinned by Nix, not by a lockfile:

```nix
tofu.providers = plugins: [ plugins.hashicorp_random ];
```

`tofu init` then resolves them from the store and needs no network.

`ekn tofu {plan,apply,destroy} --target infra` runs it. A separate verb from
`ekn kubeapply` on purpose — OpenTofu carries a backend, a lock and a
plan/apply split, and `--prune` means nothing to it. A unit's `dependencies`
run first, deepest first, each as its own `tofu` invocation: OpenTofu has no
`ekn.resourcePriority` equivalent, so there is no single plan to merge them
into. For the same reason a dependency may not cross classes, and an assertion
says so.

`ekn commit` writes each unit's `config.tf.json` beside the rendered
manifests, so an infrastructure change is reviewable as a diff. Committing it
does not make anything apply it — `ekn rollback` restores the file with the
rest of the tree, but `ekn tofu` reads the Nix evaluation rather than the
branch, so rolling infrastructure back means rolling the source back.

`ekn tofu` keeps a working directory per unit under `.ekn/tofu/<name>`, holding
`.terraform/` and, for a local backend, live state.

**Add `.ekn/` to your repository's `.gitignore` before anyone runs
`ekn tofu`.** This is a secret-handling step, not housekeeping. OpenTofu state
holds every attribute of every resource in the clear, including the ones a
provider marks sensitive — a cluster CA, machine secrets, a kubeconfig. The
directory is created by the first run, in whatever repository that run happens
in, and easykubenix cannot ignore it on your behalf. Every run prints where state actually lives, because easykubenix
has no default backend — a unit that declares none gets a local file, and
nothing else would say so.

**Destroy a `tf` unit's infrastructure before deleting the unit, never after.**
Deleting a Kubernetes unit is safe: prune selects by label, so dropping the
module deletes its objects. A `tf` unit is the opposite — nothing evaluates it
any more, so `ekn tofu destroy --target <name>` cannot even name it, and its
real infrastructure keeps running and costing money. `ekn tofu` warns when it
finds local state for a unit the evaluation no longer declares; a remote
backend's keys it cannot see, which is why the ordering is a rule and not only
a check.

OpenTofu reads `${...}` inside any JSON string as an expression, so two helpers
say which you meant:

```nix
tofu.output.name.value = ekn.lib.tf.ref "random_pet.cluster.id";
stringData.script = ekn.lib.tf.escape someShellScript;
```

Without `escape`, a string holding `${` fails inside `tofu` with a message
about an unknown variable, naming neither the option nor the Nix string behind
it.

An output never reaches Nix — evaluation would then depend on what a previous
apply did. It travels at apply time instead, which is what connects a `tf` unit
that builds a cluster to the Kubernetes unit that has to reach it:

```console
$ ekn tofu apply --target infra
$ ekn kubeapply --target apps --kubeconfig-from-tofu infra:kubeconfig
```

`--kubeconfig-from-tofu` is the single-operator and bootstrap path. Reading a
tofu output means reading that unit's state, so it needs that unit's backend
and its credentials — it couples deploying an application to owning the
infrastructure state. Where those are different people, use an ordinary
kubeconfig and keep the two apart.

See `design/opentofu.md`.

### Kluctl integration (deprecated)

`kluctl` is a CLI and GitOps tool that deploys manifests, and easykubenix can
still generate a minimal kluctl project and deployment script. It predates
`ekn kubeapply`, which now covers the same ground natively. See
[issue #2](https://github.com/Lillecarl/easykubenix/issues/2); `kluctl.*`
options still work, and `kluctl.discriminator`/`kluctl.resourcePriority` have
moved to `ekn.environment`/`ekn.resourcePriority` with warnings pointing at the
new paths.

[Documentation](https://lillecarl.github.io/easykubenix/)

---

*This project is made possible by*

[![Dynamist](.assets/dynamist-logo.png)](https://dynamist.se/)
