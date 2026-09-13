# OpenTofu in easykubenix

A deployment unit can hold OpenTofu configuration instead of Kubernetes
objects. The two kinds are told apart by the module system's class —
`class = "tf"` against `class = "kubernetes"`.

Status: the Nix half is built and gated by
`nix build --file ./checks.nix tofu-render`. The four design decisions below
are all answered. What remains is `ekn` — see the touch points at the end.

## What the class does, and what it does not do

`lib.evalModules` takes a `class` argument. It is a nominal type on the
evaluation. `collectModules` checks every module as it is imported: a module
whose own `_class` is set and does not match throws, and a module with no
`_class` imports into any evaluation. See `/etc/nixpkgs/lib/modules.nix`,
`checkModule` at line 456.

That is the whole mechanism. It is an import guard. It does **not**:

- select which base modules are merged,
- reach `config` in any form the modules can branch on,
- exist on `types.deferredModule`, which takes no `class` argument at all
  (`deferredModuleWith` in `/etc/nixpkgs/lib/types.nix`, line 1313).

The last point matters for `deployment.units.<name>.modules`. That option is a
`listOf deferredModule` and it stays one. The class check on those modules
happens later, inside the `evalModules` call `mkInstance` makes — not at the
point the unit declares them.

So the class is cheap insurance, and something else has to do the dispatch.

### What that bought

`mkInstance` passes `class = "kubernetes"`, and the base modules that declare
Kubernetes options carry `_class = "kubernetes"`. `assertions.nix` and
`lib.nix` are deliberately unmarked: neither declares a Kubernetes option, and
an unmarked module imports into any class, so a `tf` class reuses both
unchanged.

Verified two ways:

- `nix build --file ./checks.nix all` is green, so the guard costs the existing
  configurations nothing — including the nested bootstrap instance, the
  `deferredModule` path and the `mkRenamedOptionModule` aliases.
- A module carrying `_class = "tf"`, imported into the instance, fails with
  "cannot be imported into a module evaluation that expects class
  \"kubernetes\"".

The second is the entire benefit. Before it, the same mistake produced one
"option does not exist" per option, and named neither class.

## The shape

### The dispatch

One option on the unit submodule in `easykubenix/gitops.nix` does the
dispatch:

```nix
deployment.units.<name>.class = lib.mkOption {
  type = lib.types.enum [ "kubernetes" "tf" ];
  default = "kubernetes";
};
```

`mkInstance` takes a matching `class` parameter and picks the base module list
from it, over a shared `commonModules`:

```nix
baseModules = {
  kubernetes = [ ./easykubenix/assertions.nix ./easykubenix/ekn.nix ... ];
  tf         = [ ./easykubenix/assertions.nix ./easykubenix/lib.nix ./easykubenix/tofu.nix ];
};
```

Two things in the unit submodule were Kubernetes-only and would have broken a
`tf` instance. Both are conditional on the class now:

- the injected `{ ekn.environment = lib.mkDefault config.ekn.environment; }`
  module. `ekn.environment` is not declared in a `tf` instance, so this was an
  error before any user module was read.
- `config.labels."ekn.dev/deployment-unit" = lib.mkDefault name` and the two
  assertions that enforce it. A label is a Kubernetes concept, and a `tf` unit
  needs neither.

`easykubenix/kubernetes.nix` has one more. `submodulesByTarget` filters on
`t.modules != [ ]` and then reads `t.instance.config.kubernetes`, which does
not exist in a `tf` instance. It filters on the class too.

A fourth was caught by nothing at all. Once a `tf` unit is declared, a
Kubernetes object can route itself to it with
`ekn.deploymentUnit = "<tf unit>"`: `declared` resolves, `submodulesByTarget`
filters the unit out, and the result is a `kubernetes.deploymentUnits` entry
holding routed objects with no instance behind them — wrong, and silent.
`misroutedObjects` now asserts against it.

### Where the rendered result lands

A `tf` unit does not appear in `kubernetes.deploymentUnits`.

That attrset is a published schema. `ekn/src/ekn/eval.py` validates it with
pydantic (`GitOpsTargetEntry` requires `objects`), and `tests/test_gitops.py`,
`nix/kubeapply/manifests.nix` and `docs/examples/bootstrap/default.nix` all
read `kubernetes.deploymentUnits.<name>.objects`. `default.nix` says the rule
itself: a module change needs a matching `ekn` change, and owning both makes
that one commit. Widening the schema so half its entries carry no `objects`
buys nothing and costs every consumer a branch.

`deployment.tofuUnits` is the parallel output, and keeps the Kubernetes schema
untouched. It carries each unit's `path`, its dependency closure, and the store
paths of its rendered `config.tf.json` and its wrapped `tofu` binary.

### The option tree

OpenTofu reads JSON configuration natively, so there is no HCL to generate. A
`tofu.nix` declares `tofu.{terraform,provider,resource,data,module,output,
variable,locals,check,ephemeral,import,moved,removed}` over a recursive JSON
value type, and renders one `config.tf.json` — pretty-printed through
`jq --sort-keys`, because decision 4 commits it to a branch for a person to
read.

That tree already exists as terranix, which does exactly this and has for
years. Two findings about reusing it:

- **Licence.** terranix is GPL-3.0. nixidae is Apache-2.0. Importing terranix'
  modules as a library is a conflict this repository should not walk into
  without a decision. Mirroring the option tree — which is a transcription of
  Terraform's own JSON schema, not terranix' invention — is the safer route,
  and the shape is small: ten options, all the same recursive value type.
- **The `${` trap, worth copying.** Terraform treats `${` in *any* JSON string
  as an interpolation. So every Nix string that happens to contain `${` and is
  not meant as a Terraform reference has to be emitted as `$${`. terranix
  handles the other direction with a helper (`lib/tf.ref` wraps a string *into*
  `${...}`) and leaves escaping to the author. Getting this wrong fails at
  `tofu` parse time with a message about an unknown variable, which does not
  point at the Nix string that produced it.

Providers come from Nix: `pkgs.opentofu.withPlugins (p: [ p.hashicorp_random ])`
wraps `tofu` so `init` resolves plugins from the store with no network. nixpkgs
has a test for exactly this (`opentofu_plugins_test` in
`/etc/nixpkgs/pkgs/by-name/op/opentofu/package.nix`) that runs `tofu init`
with the proxy pointed at a dead port. That is the pinning story, and it is
better than a lockfile.

## The four decisions, and what they settled

Each of these was a fork that led to materially different work. All four are
answered now.

### 1. A separate verb applies a `tf` unit

`ekn tofu {plan,apply,destroy} --target X`, not a class dispatch inside
`ekn kubeapply`.

A Kubernetes apply is idempotent and needs no state. `tofu apply` needs a
backend, a lock and a plan/apply split. On `kubeapply` every flag would have
meant two things: `--prune` has no tofu sense at all, and `--dry-run` would be
`plan` on one class and a server-side dry run on the other. `destroy` has no
Kubernetes analogue and no safe place on that command.

### 2. A tofu output never reaches Nix

Not at evaluation, in any form. Reading `tofu output -json` during evaluation
is import-from-derivation against live infrastructure state, and it would make
evaluation depend on what a previous apply did.

Substitution at *apply* time is the direction left open, and the concrete case
is a kubeconfig: a `tf` unit provisions the cluster, and the `ekn kubeapply`
that follows has to reach it. `ekn kubeapply --kubeconfig-from-tofu
<unit>:<output>` does that by running `tofu output` and pointing `KUBECONFIG`
at the result, with nothing written to `/nix/store` and nothing read during
evaluation.

The same mechanism generalises later if it needs to. `ekn.envSeed "VARNAME"`
already puts a reference in a manifest and resolves it from the environment at
apply time (`ekn/src/ekn/seeds.py`), so seeding those variables from a `tf`
unit's outputs is the natural next step — but it is not built, because the
kubeconfig case is the one that came up.

### 3. A dependency may not cross classes

A `tf` unit is a separate module-system invocation with a separate `tofu` run
behind it, and OpenTofu has nothing like `ekn.resourcePriority` — ordering
inside a unit is OpenTofu's own dependency graph.

So `dependencies` stays within a class, and an assertion says so. A Kubernetes
apply merges every contributing unit's objects into one plan ordered by
priority; a `tf` apply is a chain of separate invocations, deepest first.
Neither can hold the other.

Getting a `tf` unit down before the Kubernetes unit that needs it is therefore
two commands, and decision 2's kubeconfig bridge is what connects them.

### 4. `ekn commit` writes the rendered `config.tf.json`

So the configuration diff is visible in the branch, the same way the rendered
manifests are.

This is what makes `config.tf.json` pretty-printed rather than the one line
`builtins.toJSON` produces: a one-line diff of a whole infrastructure change
says only that it changed.

## Ownership and pruning

No analogue is needed. The two labels (`ekn.dev/environment`,
`ekn.dev/deployment-unit`) exist because Kubernetes has no record of what a
previous apply produced, so easykubenix has to write one onto the objects
themselves. Tofu's state file is that record, and it is authoritative. Inventing
a second one would create two answers to the same question.

The one thing worth carrying over is the *scope* rule: one state file per unit,
named by the unit, so a `tf` unit's blast radius is its own and an apply of one
unit can never destroy another's resources.

## Touch points, in dependency order

Done, in the Nix half:

1. `default.nix` — `mkInstance` takes `class`; `baseModules` is an attrset
   keyed by it, over a shared `commonModules`.
2. `easykubenix/gitops.nix` — the unit `class` option; the injected
   environment module and the `ekn.dev/deployment-unit` label default are
   `kubernetes`-only, and so are the two assertions over that label.
   `dependencyClosure` lives here now, because both classes need it.
   `deployment.tofuUnits` publishes what a `tf` unit rendered, and
   `crossClassDependencies` asserts decision 3.
3. `easykubenix/kubernetes.nix` — `submodulesByTarget` filters on class, and
   `misroutedObjects` asserts that nothing routes an object into a unit of
   another class.
4. `easykubenix/tofu.nix` — `_class = "tf"`, the option tree,
   `tofu.generated` and a pretty-printed `config.tf.json`. `nix/tofu` is its
   gate: it diffs the render against a literal, then runs `tofu init` and
   `tofu validate` in the build sandbox, where there is no network to fall
   back on.

Remaining, in `ekn`:

5. `ekn tofu {plan,apply,destroy} --target X` — decision 1. Reads
   `deployment.tofuUnits`, realises each unit's `configFile` and `tofu`, and
   runs the dependency closure deepest first, each as its own invocation.
   The store path is read-only, so `config.tf.json` is copied into a per-unit
   working directory; `tofu init` writes `.terraform/` and a lock file beside
   it.
6. `ekn commit` writes each `tf` unit's `config.tf.json` to its `path` —
   decision 4. `gitops.file_groups` currently raises when
   `kubernetes.deploymentUnits` is empty, which a `tf`-only instance would
   hit.
7. `ekn kubeapply --kubeconfig-from-tofu <unit>:<output>` — decision 2. Runs
   `tofu output -raw` on the named unit and points `KUBECONFIG` at the
   result, which is how a Kubernetes unit reaches a cluster its `tf`
   dependency just built.

Items 1 through 4 needed no change to the `ekn` CLI or to the JSON schema it
validates, which is why they went first.
