# OpenTofu in easykubenix

A deployment unit can hold OpenTofu configuration instead of Kubernetes
objects. The two kinds are told apart by the module system's class —
`class = "tf"` against `class = "kubernetes"`.

Status: built, on both sides, and migrated once. The four design decisions
below are answered and implemented, and a first adopter has moved a live
25-resource tree onto a `tf` unit — render byte-identical, state adopted
cleanly. `nix build --file ./checks.nix tofu-render` is the gate.

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

## What review changed

The five edges a second reader pushed on, and what each one turned into.

**The deleted lock hid a provider bump.** Dropping `.terraform.lock.hcl` costs
no integrity — its hashes verify what the store already guarantees — but it did
carry a signal: notice that a provider moved. `config.tf.json` records
constraints, not resolutions, so a 5.31 to 5.40 bump crossed a reviewed diff
invisibly. `providers.json` now carries the name, version and store path, and
is committed beside the configuration. The signal moved from apply time to
review time, which is strictly better than keeping the lock.

**`destroy` guarded the wrong direction.** `apply` walks the closure deepest
first, so it cannot run against a half-built dependency. `destroy` had nothing
equivalent: it refused to cascade, which was right, but let you destroy a unit
another still needed. It now computes the reverse closure and refuses, naming
what blocks it — which also answers the ordering a teardown needs, without the
tool ever cascading.

**A deleted unit leaked money.** This is the one place the labels-versus-state
asymmetry bites. Delete a Kubernetes unit and prune deletes its objects; delete
a `tf` unit and nothing can name its infrastructure any more. `ekn tofu` warns
on local state with no declaring unit, and the ordering rule — destroy before
delete, never after — is documented, because a remote backend's keys are not
ours to enumerate. The scan reports what it checked on a clean run as well, for
the reason in the next section.

**No default backend was right; an assertion would not have been.** The gate is
the proof: `checks.nix` runs `tofu init` in the sandbox, and a mandatory backend
would force a fake block into every fixture to satisfy a rule about production.
Every run prints where state actually lives instead — apply-time truth, not an
evaluation-time guess.

**`--kubeconfig-from-tofu` is the bootstrap path.** There is no cheaper route to
`tofu output` — it reads state, state needs the backend, `init` is how you get
one — so the side effect stays. What changed is honesty about the coupling:
reading that output needs the infra unit's backend and credentials, so wherever
deploying apps and owning infra state are different people, an ordinary
kubeconfig is the answer. `init` is now also skipped when it would change
nothing, which is most of what that path was paying for.

## Adopting an existing tree

Every real adopter is migrating a tree that already exists and already holds
live state. The greenfield case above is the easy one; this is the other end of
the same asymmetry the "deleted unit leaked money" section reasons about.

**The cutover is a file copy.** Put the existing `terraform.tfstate` into
`.ekn/tofu/<unit>/` before the first run. `prepare` creates the directory,
copies the rendered configuration in and runs `tofu init`, which adopts the
state already sitting there. Measured end to end: a unit whose state was
dropped in beforehand plans `No changes. Your infrastructure matches the
configuration.` rather than planning to create what already exists.

Confirmed against a real tree and not only a fixture. The first adopter
migrated 25 resources across three providers — hashicorp/kubernetes,
gavinbunney/kubectl and siderolabs/talos — describing five running Talos VMs,
their cloud-init Secrets, machine configurations, bootstrap and kubeconfig.
Every resource refreshed and matched on the first run; nothing planned for
creation or replacement. No provider wanted re-initialising against the new
working directory and no resource id had drifted. "It is just a copy" is worth
believing with that attached.

Nothing special is needed for it, and that is deliberate — a `tf` unit's
working directory is an ordinary OpenTofu working directory. The one thing to
get right is the order: copy the state in *before* the first `ekn tofu`
command, not after a run has already written an empty one.

That ordering is the only sharp edge in the migration, so `prepare` says it
rather than leaving it to this document. A first run with a local backend and
no state file prints:

```console
infra: no existing state, so this plans as if nothing exists yet.
Adopting an existing tree? Copy its terraform.tfstate into .ekn/tofu/infra first.
```

Self-limiting — after the first apply the file exists and the line stops — and
local backends only, since a remote one keeps no local file and its absence
says nothing. It is the same principle as the orphan scan: an empty state this
run just created and an empty state that was always right look identical from
anywhere downstream, so it has to be said where the difference is still
knowable.

**Delete the copy if you were only rehearsing.** The tool cannot help with this
one and the danger is worse than the empty case. A rehearsal leaves a state
file behind; the original tree keeps running and moving; and the next person to
run the unit adopts a *stale* copy. That plan looks clean — it is internally
consistent — right up until it silently reverts whatever changed in between. An
empty state at least plans to create everything, which is loud. Until the real
cutover, the original tree is the only authority, and a second copy under
`.ekn/` is precisely what somebody adopts by accident later.

Prove the render before touching state. `config.tf.json` is a build artefact,
so an adopter can diff it against what the old tree produced and know the
conversion is faithful without going near a cluster.

### Two version questions, and only one of them is real

Both measured against OpenTofu 1.12.5, because both come up the moment a tree
that pinned its own nixpkgs moves into somebody else's evaluation.

**`terraform_version` in the state file is not enforced.** A state recording a
*newer* OpenTofu is read, planned and applied without complaint, and the
version is quietly rewritten down on the next write. Checked at 1.12.9, 1.13.0
and 2.0.0 against a 1.12.5 binary: all three planned, and an apply rewrote the
field to 1.12.5. So a patch-level difference between the tree's old pin and its
new one — the common case — is not the blocker it looks like.

**The state *format* version is enforced.** Bumping `version` from 4 to 5
fails, and usefully:

```
Error: Error acquiring the state lock
failed to write backup file: Unsupported state file format:
The state file uses format version 5, which is not supported by OpenTofu
```

That is the real compatibility boundary, and it moves far more rarely than the
binary version does.

One measurement recorded without explanation: `required_version` in the
`terraform` block was *not* enforced either, in HCL or in JSON — a 1.12.5
binary initialised and planned against `>= 1.99.0`. That contradicts what the
constraint is for, and this note does not claim to know why. Do not rely on it
either way.

## Silence must not carry a meaning it did not earn

A rule this repository already broke twice while the OpenTofu work was being
written, both times the same shape, so it is worth stating once rather than
fixing a third time.

The orphaned-state scan printed nothing when it found nothing. So did a scan
that could not look — it never sees a remote backend. A reader turns the same
empty output into "no orphaned state", which is stronger than the tool can
claim. It now names what it examined on every run, and says what it did not:

```console
checked 3 local working directories, none orphaned (remote backends not inspected)
```

The state-location line had it the other way round. It was printed after the
`init` that a warm run skips, so it disappeared exactly when runs are cheap and
repeated — which is when somebody is most likely watching. Its absence read as
"nothing to say" rather than "not reached".

The rule both cases want: **an output whose absence is meaningful must be
produced on the path where nothing happened, not only where something did.** A
tool that states what it did not look at is more trustworthy than one that says
nothing, and far more than one that appears to have looked at everything.

## An inherited guarantee is still a dependency

Separate from the silence rule above, and worse in one specific way.

`latestWhere (v: lib.versionOlder v "1.0.0")` selected a beta of a provider.
The bound was correct before the move to the OpenTofu registry and correct
after it. What changed was the *source*: nixpkgs never packaged a prerelease,
so a version bound had been doing the work of a stability filter without
anybody writing one. Moving to a complete index removed a constraint that was
never stated anywhere, because it was a property of what we were reading rather
than of what we had written.

That is why it could not be caught by reading the code. The other failures in
this note are two states that look identical at a moment — look harder and you
find them. Here nothing was wrong at the moment of the change, and the
expression that depended on the guarantee never mentioned it. There was nothing
to read.

The evidence it is easy to hit: the same trap was found three times, by two
people who could not see each other's fixes, and all three reached the same
`v: !(lib.hasInfix "-" v)` predicate. That is what moved the filter into
`latest`/`latestWhere` rather than into either project's documentation — a
guarantee nobody names cannot be restored by a sentence telling people to
remember it.

**The question worth asking when a source gets more complete: what was the
narrower one filtering that nothing asked it to?**

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
   `tofu.generated` and a pretty-printed `config.tf.json`, plus
   `tofu.resolvedProviders` rendering `providers.json` beside it. `nix/tofu`
   is its gate: it diffs the render against a literal, checks the resolved
   provider, then runs `tofu init` and `tofu validate` in the build sandbox,
   where there is no network to fall back on.

Done, in `ekn`:

5. `ekn tofu {plan,apply,destroy} --target X` — decision 1. `evaluate_tofu_units`
   reads `deployment.tofuUnits` and realises each unit's `configFile` and
   `tofu`; `ekn/tofu.py` copies the configuration out of the read-only store
   into a per-unit working directory, drops the stale `.terraform.lock.hcl`,
   and runs the closure deepest first. `destroy` refuses while `dependents`
   is non-empty.
6. `ekn commit` writes each `tf` unit's `config.tf.json` and `providers.json`
   to its `path` — decision 4. `gitops.file_groups` no longer raises on an
   empty `kubernetes.deploymentUnits`, and the "nothing to commit" check moved
   to where both halves are known.
7. `ekn kubeapply --kubeconfig-from-tofu <unit>:<output>` — decision 2. The
   output lands in a 0600 temporary file, passed straight to `kr8s`, removed
   when the command ends.

What every `ekn tofu` run says about itself, all of it from review or from the
first adopter: `state_location` names where state lives, `orphaned_state`
reports what it scanned and what it could not see, and `prepare` says when
there is no state to adopt. Each is in `ekn/tofu.py`; the reasoning for all
three is one section up.

Items 1 through 4 needed no change to the `ekn` CLI or to the JSON schema it
validates, which is why they went first.

`kubernetes.deploymentUnits` is untouched by all of this. A `tf` unit lives in
`deployment.tofuUnits` and validates through its own `TofuUnit` model, so every
consumer that reads `.objects` off a Kubernetes unit still reads exactly what
it did before.
