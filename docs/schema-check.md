# Schema check

`ekn schema-check` validates every rendered object the way the API server
would. It finds a typo, a wrong type or a failed policy before anything is
applied.

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
