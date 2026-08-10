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

Demonstrates the `mkNamedList` and `mkNumberedList` helpers for overriding
container lists and environment variables by name instead of positional index.

```{literalinclude} ./examples/namedlists/default.nix
:language: nix
```

`initContainers` use `mkNumberedList`, which addresses an entry by its index.
Regular `containers` and `env` entries use `mkNamedList`, which addresses an
entry by its `name` attribute. Both helpers keep the order of the entries that
a plain list already defines.

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
entry at the end. Use `mkNumberedList` to address an entry by index instead,
for example a list of scalars such as `args`.

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
priority — apply last and are validated against the live schema. The `apiMappings` option tells easykubenix what
apiVersion to use for each custom kind.
