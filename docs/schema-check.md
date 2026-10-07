# Schema check

`ekn schema-check` validates every rendered object the way the API server
would. It finds a typo, a wrong type or a failed policy before anything is
applied. The same validator checks each OpenTofu unit's `config.tf.json`; see
OpenTofu units below.

## What it checks

1. **JSON schema.** Each object is checked against its kind's schema. Field
   validation is strict: an unknown field is a violation, as with
   `kubectl apply --validate=strict`.
2. **Custom resource metadata.** A CRD types `metadata` only as `object`. The
   API server still decodes it as ObjectMeta, so the check does too.
3. **CRD CEL rules.** Each `x-kubernetes-validations` rule runs with `self`
   bound to its schema node. Schema defaults are filled first, as the API
   server fills them.
4. **ValidatingAdmissionPolicies.** Each binding runs its policy against the
   rendered objects it matches: `matchConstraints`, `matchResources`,
   `variables`, `matchConditions` and `validations`.

## Where the schemas come from

| source | used when |
| --- | --- |
| a CRD in the render | always; it replaces every other source for its kinds |
| the cluster's `/openapi/v3` | the cluster answers |
| yannh/kubernetes-json-schema | the cluster does not answer |
| `validation.openapiSpec`, the pinned Kubernetes source tree | `--offline`, the Nix build, and the kinds yannh lacks |

A rendered CRD wins because it is what the apply installs. Server and yannh
schemas are cached under `$XDG_CACHE_HOME/ekn/schemas/<cluster>`, keyed by
the `kube-system` namespace uid.

In a live run, the check also reads the cluster's installed admission
policies, their bindings and its Namespaces. A rendered object with the same
kind and name replaces the installed one.

## Where it runs

- `ekn schema-check`: against the cluster, with the yannh fallback.
  `--offline` reads the pinned spec and reaches no network.
- `ekn render --validate`: the offline check on the render. Violations go
  to stderr, and nothing is written.
- `ekn kubeapply`: the live check, after the cluster fence and before the
  first write. `--skip-schema-check` turns it off.
- `config.validation.schemaCheck`: a Nix build of the offline check. It
  runs in the build sandbox, so it can gate CI.

Each run logs how long it took: `load` reads the schemas, `check` runs the
schemas and CRD rules, `policies` runs the admission policies.

## What it cannot know

A render is not a request, so some CEL inputs do not exist. An expression
that reads one is skipped and counted, never guessed:

- `request` and `authorizer`.
- `params`, when a policy declares a `paramKind`.
- `oldSelf` in a CRD rule, and `oldObject` in a policy that admits only
  UPDATE. An apply is evaluated as a CREATE, with `oldObject` null.
- `namespaceObject`, for a namespace that neither the render nor the cluster
  holds.
- A Kubernetes CEL extension function that cel-python does not have.

The log lists every skip by reason. An expression that fails to evaluate is a
violation, as it is on the API server, unless the policy says
`failurePolicy: Ignore`.

The API server also fills defaults on built-in kinds before admission, in Go
code that no schema describes. A policy expression that reads such a field
without `has()` can fail here and pass on the server.

## OpenTofu units

A `tf` unit's `config.tf.json` is checked against a JSON Schema, by the same
validator as the manifests. The schema has two sources:

| source | build |
| --- | --- |
| OpenTofu's core schema: `terraform`, `variable`, `output`, the meta-arguments | opentofu-schema, the library the OpenTofu language server reads, exported by `tools/tofuschema` for `tofu.package`'s version |
| each provider's resources, data sources and configuration | `tofu providers schema -json` in the build sandbox, as `tofu.providerSchemas` |

`tofu.providerSchemas` reads only `required_providers`, so a change to a
resource does not rebuild it. `ekn.tofuschema` merges the two into
`tofu.jsonSchema` and writes the rules of HCL's JSON syntax into it: a
`${...}` template is accepted for any value, `"2"` is a number, `"1"` is a
bool, and a block is one object or an array of them.

The check refuses an unknown argument or block, a missing required argument,
a value that does not convert to its type, a read-only attribute, a resource
or data type that no required provider declares, and a provider that
`required_providers` does not name.

It runs in four places:

- `ekn tofu plan|apply|destroy`: before the first `tofu init`, for every
  unit in the chain. `--skip-schema-check` turns it off.
- `ekn kubeapply`: before the cache push and the first write, for every unit
  that an applied object names in its `ekn.dev/tofu-units` annotation.
  `--skip-schema-check` turns it off.
- `ekn deploy`: in the verify stage, for every `tf` unit.
- `tofu.schemaCheck`: a Nix build of the same check.

An object that hands a unit to something in the cluster carries the
annotation. An operator's custom resource that names the unit's `configFile`
is an example. The value is a comma-separated list of unit names, and
`ekn.lib.tf.unitsAnnotation` holds the key:

```nix
metadata.annotations.${ekn.lib.tf.unitsAnnotation} = "day2";
```

kubeapply reads only the annotation, never the rest of the object. An
evaluation that names a unit which is not of class `tf` fails, so a typo
cannot skip the check.

`nix build --file ./checks.nix tofu-schema` holds the check to
`tofu validate`, case by case. No deploy path runs `tofu validate`.

### Where it differs from `tofu validate`

It is stricter in three places:

- An unknown argument in a `provider` block. `tofu validate` does not read the
  block, and `tofu plan` refuses it.
- An unknown argument in the backend. `tofu validate` does not read it, and
  `tofu init` refuses it.
- An unknown key in a nested attribute's object. OpenTofu drops the key
  silently, so it does nothing.

It cannot know:

- A limit that a provider enforces in its own code and not in its schema.
  The `tls` provider's `subject` block, at most one, is an example.
- A module call's inputs. They come from the called module, so a `module`
  block takes any argument.
- The type of a variable's `default`.
- What a `${...}` template evaluates to.

A backend argument the schema requires must be in `config.tf.json`. `ekn
tofu` runs `tofu init` with no `-backend-config`, so nothing else can give
it, and an argument that the backend reads from the environment is refused
as missing.

opentofu-schema 0.4.3 describes OpenTofu up to 1.12. A newer `tofu.package`
gets the 1.12 core schema.
