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

Demonstrates the `_namedlist` and `_numberedlist` helpers for overriding
container lists and environment variables by name instead of positional index.

```{literalinclude} ./examples/namedlists/default.nix
:language: nix
```

`initContainers` use `_numberedlist` (order-preserving), while regular
`containers` and `env` entries use `_namedlist` (keyed by the `name` attribute).

---

## Helm Charts

Integrates an external Helm chart (`ingress-nginx`) into the easykubenix
module tree. Chart objects are injected into the `kubernetes.objects` namespace
and run through the same transformer/generator pipeline.

```{literalinclude} ./examples/helm/default.nix
:language: nix
```

The `fetchHelm` function downloads and renders the chart. Set
`convertLists = true` (the default) to make chart values overridable by name.

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
