# Examples

All source files live under `docs/examples/` and are also run as CI checks.
Each example calls `easykubenix` to produce Kubernetes manifests and verifies
the output is valid JSON/YAML.

## Basic Resources

Creates a ConfigMap, Secret, Pod, Deployment, and Service in the `default`
namespace — the fundamental building blocks of any Kubernetes project.

```{literalinclude} ./examples/basic/default.nix
:language: nix
```

The generated manifest will contain 5 items with `apiVersion`, `kind`, and
`metadata` automatically populated by easykubenix.

---

## Named Lists

Demonstrates the `mkNamedList`, `mkNumberedList` and `mkReplaceList` helpers for
overriding container lists, environment variables and flags without writing the
whole list again.

```{literalinclude} ./examples/namedlists/default.nix
:language: nix
```

`initContainers` use `mkNumberedList`, which addresses an entry by its index.
Regular `containers` and `env` entries use `mkNamedList`, which addresses an
entry by its `name` attribute. The container's `args` use `mkReplaceList`, which
addresses an element by the start of its own value. All three keep the order of
the entries that a plain list already defines.

`mkReplaceList` is for a list whose elements have neither a `name` nor an index
that survives the next chart version, which is what a container's `args` is. The
key is the start of the element, and the value takes the whole element. A key
that matches no element is an error, and so is a key that matches more than one.
A replacement needs no `mkForce`: it replaces an element rather than defining
one.

`mkReplaceList` takes an attribute set. Use `lib.mkMerge` to compose several of
them, or to hold a list and a patch of it in one definition:

```nix
args = lib.mkMerge [
  (lib.mkReplaceList { "--metrics-addr=" = "--metrics-addr=:8443"; })
  (lib.mkIf cfg.debug (lib.mkReplaceList { "--v=" = "--v=4"; }))
];
```

### Matching with a predicate

A prefix only reads a string. For a list of objects with neither a `name` nor a
stable index — `tolerations` is the usual one — `lib.mkReplaceWhere` addresses
an element with a function.

Say the chart renders three of them, and only the second is wrong:

```nix
tolerations = [
  { key = "dedicated";                            operator = "Exists"; }
  { key = "node-role.kubernetes.io/control-plane"; operator = "Exists"; effect = "NoSchedule"; }
  { key = "node.kubernetes.io/unreachable";        operator = "Exists"; tolerationSeconds = 30; }
];
```

`where` runs against each element in turn and must pick exactly one:

```nix
tolerations = lib.mkReplaceWhere {
  control-plane = {
    where = toleration:
      (toleration.key or null) == "node-role.kubernetes.io/control-plane";
    value.effect = "NoExecute";
  };
};
```

The render keeps all three, in order, with one field of the second changed:

```nix
tolerations = [
  { key = "dedicated";                            operator = "Exists"; }
  { key = "node-role.kubernetes.io/control-plane"; operator = "Exists"; effect = "NoExecute"; }
  { key = "node.kubernetes.io/unreachable";        operator = "Exists"; tolerationSeconds = 30; }
];
```

`operator` survived because the body did not name it, and `effect` changed with
no `mkForce`. A `where` that matches none of the three, or two of them, is an
error naming the option and printing the list.

The attribute name is a label. It names the replacement in an error, and it is
what two definitions of the same replacement merge on; it is not matched against
anything. `mkReplaceList` is the short form of this: a label with no `where`
matches by the start of its own name, and both forms share one marker, so they
compose on one field.

`value` **merges over the element it matched.** The element joins the merge as a
default, leaf by leaf, so ordinary module-system rules decide the rest:

| you write | you get |
| --- | --- |
| `value.effect = "NoExecute"` | that field changed, every other field of the element untouched, no `mkForce` needed |
| a field the element does not have | added |
| `value = lib.mkForce { ... }` | the element dropped and the body in its place, **losing every field the body does not name** |
| a string element | replaced, because a string is a single leaf |
| two modules setting one field | a conflict that names the field |

A second module overrides a replacement by writing the label with no `where`,
because two predicates for one label cannot be merged:

```nix
tolerations = lib.mkReplaceWhere {
  control-plane.value.effect = lib.mkForce "PreferNoSchedule";
};
```

---

## Conditional Definitions

Demonstrates `mkIfExists` and `mkIfExistsAtPath`, which patch an object only
when that object is already there.

```{literalinclude} ./examples/conditional/default.nix
:language: nix
```

An ordinary definition creates the key it names. So a patch written against a
chart resurrects the object the chart stopped shipping, as a fragment with no
containers. A conditional definition contributes only when an ordinary
definition for that exact key survives; otherwise it contributes nothing.

`kubernetes.objects` is conditional at the namespace, the Kind and the object
name, and an object's own fields are conditional too. So the rule holds at
every level: a conditional namespace that exists still adds children to
itself, and a conditional Kind that does not exist creates neither itself nor
anything under it.

`mkIfExistsAtPath "namespace.Kind.name" value` is the path form, and all three
levels must exist. Pass a list, `[ "namespace" "Kind" "name" ]`, for a key that
contains a dot. Put a priority inside the marker content, as
`mkIfExists { replicas = lib.mkForce 3; }`, never around it.

An `mkIf false` definition does not count as existing. Neither does a key that
only other markers define.

---

## Helm Charts

Integrates an external Helm chart (`ingress-nginx`) into the easykubenix
module tree. Chart objects are injected into the `kubernetes.objects` namespace
and run through the same transformer/generator pipeline.

```{literalinclude} ./examples/helm/default.nix
:language: nix
```

The `fetchHelm` function downloads and renders the chart. A chart list stays a
plain list. To override one entry of it by name, give the same field an
`mkNamedList` value in your own module. The attribute name selects the entry:

```nix
kubernetes.objects.default.Deployment.my-chart.spec.template.spec.containers =
  lib.mkNamedList { main.image = lib.mkForce "my-registry/main:1.2.3"; };
```

The type merges the two definitions by name. An entry keeps the position it had
in the chart output. An attribute name that the chart does not use adds a new
entry at the end. Use `mkNumberedList` to address an entry by index instead, and
`mkReplaceList` to address one element of a list of strings by the start of its
own value:

```nix
kubernetes.objects.default.Deployment.my-chart.spec.template.spec.containers =
  lib.mkNamedList {
    main.args = lib.mkReplaceList { "--metrics-addr=" = "--metrics-addr=:8443"; };
  };
```

---

## Generators, Transformers & Filters

Showcases the three extension pipelines:

- **Generators** — create new objects from existing ones (auto-VPA for Deployments)
- **Transformers** — modify objects in-place (annotations on Services)
- **Filters** — remove objects matching conditions (exclude Pods)

```{literalinclude} ./examples/generators/default.nix
:language: nix
```

The Pod `should-be-filtered` is removed by the filter. The Service gets
annotations. The Deployment spawns a matching VerticalPodAutoscaler.

---

## Edge Cases

Tests cluster-scoped resources (`Namespace`, `ClusterIssuer`, `Certificate`),
custom `apiMappings` for non-standard kinds, and the `none` pseudo-namespace
for resources without a namespace.

```{literalinclude} ./examples/edge-cases/default.nix
:language: nix
```

Note that `none.Namespace`, `none.ClusterIssuer`, and `none.Certificate` are
cluster-scoped (no `metadata.namespace`). The `kube-system` ConfigMap
demonstrates a resource in an existing namespace.

---

## Validation with Real kube-apiserver

Spins up etcd + kube-apiserver, applies all manifests with `ekn`, dumps the
live OpenAPI v2 schema, and runs kubeconform against every resource — including
CRDs from a Helm chart and custom resources that depend on them.

The apply goes through the same `apply_and_prune` that `ekn kubeapply` uses to
bootstrap a real cluster, so the gate exercises the path you actually deploy
with rather than a second one maintained alongside it.

```{literalinclude} ./examples/validation/default.nix
:language: nix
```

The kube-prometheus-stack chart bundles `CustomResourceDefinition` objects
(Prometheus, ServiceMonitor, Alertmanager, etc.). `ekn.resourcePriority` puts
these in an earlier barrier and waits for each to become Established, after
which their dependent custom resources — being kinds with no configured
priority — apply and are validated against the live schema. The chart's
admission webhook configurations apply after those, in the last barriers of
all: nothing can serve a webhook in this harness, so registering one before
the custom resources would stall every one of them for its full timeout. The `apiMappings` option tells easykubenix what
apiVersion to use for each custom kind.

## Bootstrapping GitOps

A GitOps engine cannot sync itself into existence. The controller, its CRDs and
the root Application that points it at a branch all have to be applied before
anything can be synced — and then never again by hand.

`deployment.units.<name>.modules` is how that is expressed: a whole separate
easykubenix configuration attached to one target, rendered into that target's
path and kept out of `kubernetes.generated`. A plain `ekn kubeapply` and
`ekn validate` never see it; only `ekn kubeapply --target bootstrap` applies it.

```{literalinclude} ./examples/bootstrap/cluster.nix
:language: nix
```

`fieldManager` and `annotations` above are the handover. Applying as
`argocd-controller` rather than as `ekn` is what lets server-side apply transfer
the fields: SSA only drops a field when its *owning* manager stops declaring it,
and a bootstrap apply never runs again, so a distinct `ekn` manager would keep
owning everything ArgoCD does not declare, forever. The tracking annotation is
the separate question of whether ArgoCD considers the object its own at all —
and it is per-object, because the id encodes the object's own
group/kind/namespace/name. `ekn.lib.argocdTrackingId` builds it and declines
CRDs, which ArgoCD never stamps and which would otherwise sit permanently
OutOfSync.

The bootstrap instance itself installs ArgoCD from its chart, declares the root
Application, and — the other half of the handover — a second Application that
adopts the bootstrap folder, so upgrading ArgoCD stops being a manual step:

```{literalinclude} ./examples/bootstrap/argocd.nix
:language: nix
```

`parent` is the outer instance's evaluated config. That is what keeps the root
Application's `targetRevision` and `path` in agreement with the branch and
target the parent actually commits to, instead of the two being written out
twice and drifting. It reaches user-declared options too — `clusterRepoURL`
above is declared by `cluster.nix`, not by easykubenix. Read the parent's
*inputs* through it; reading its rendered outputs (`kubernetes.generated`,
`kubernetes.deploymentUnits`) closes a loop back through the nested instance and
recurses.

Because the nested instance is a complete easykubenix instance, it has its own
`validationScript` — which is the only way these objects get an API server at
all, `kubernetes.generated` excluding them by design:

```
nix run --file ./nix packages.bootstrapValidationScript
```

That applies ArgoCD's three CRDs, waits for them to become Established, and
then applies the `Application` that depends on them — in a later barrier,
because an unlisted kind sorts at 1000 while `CustomResourceDefinition` sorts
at 150. Pruning scopes itself to `ekn.dev/deployment-unit=bootstrap`, the
unit's own rendered label, so a bootstrap apply can never reach anything the
main configuration owns.
