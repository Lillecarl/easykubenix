#!/usr/bin/env python3
"""`ekn _applyManifest` against a real single-node kubeadm cluster.

The unit tests under ../../tests describe this code against a double, and
the validation gate applies the same manifests to a bare etcd and
kube-apiserver.  Neither can answer the questions here, because both are
missing the controllers:

    established    a CustomResourceDefinition and a custom resource of its
                   kind in one apply, with the CR read back afterwards --
                   apiextensions has to have served it
    hyphenated     that kind's singular carries a hyphen, which is the
                   shape `ekn.apply.discover` exists for
    workload       the Deployment reaches Ready, so a kubelet answered
    idempotent     a second apply of the same generation changes nothing
    prune          the next generation deletes exactly what it dropped,
                   and nothing in another environment
    prune scope    a whole-instance prune leaves a deployment unit's objects
                   alone, and a unit's own prune leaves the instance's alone
    prune gap      the documented limitation, written down as it behaves

Everything runs inside the guest.  It sees the host /nix/store over hostfs,
so `ekn` and the manifests are already there; ../kubeapply/default.nix puts
their store paths in `settings`, which is what makes the derivation build
them.
"""

from uml_runner import MachineError, run_test
from uml_runner.cluster import bring_up, get_json, kubectl, wait_for_pods

# An apply is seconds of work against an idle API server.  This is not that
# bound: the first one waits for a CRD to become Established and for a
# Deployment to be admitted, on a control plane that is a userspace process.
APPLY_TIMEOUT = 15 * 60

ENV_LABEL = "ekn.dev/environment"
UNIT_LABEL = "ekn.dev/deployment-unit"

# The kinds each generation puts on the cluster, by the name kubectl knows
# them as.  `widget-parts` is the plural of the custom kind; asking for it by
# that name is itself part of the test, since only a served CRD resolves it.
RESOURCES = ("namespaces", "configmaps", "crds", "deployments", "widget-parts")


def selector(environment, unit=None):
    """The label selector one apply owns.  Mirrors `apply.prune_selector`.

    Written out again here rather than imported, deliberately.  A test that
    asks the code under test what it prunes by cannot catch that answer being
    wrong; this one asks the API server the same question independently.
    """
    if unit is None:
        return f"{ENV_LABEL}={environment},!{UNIT_LABEL}"
    return f"{ENV_LABEL}={environment},{UNIT_LABEL}={unit}"


async def apply(cp, settings, generation, environment, unit=None):
    """Run one `ekn _applyManifest` in the guest, and return its output.

    The output is printed whether the apply passes or fails.  `ekn` logs a
    line per barrier and a line per pruned object, and that is the record of
    what the apply did -- an hour-long gate whose build log says only
    "passed" answers nothing when somebody asks it later.
    """
    scope = environment if unit is None else f"{environment}/{unit}"
    print(f"[kubeapply] applying {generation} as {scope}", flush=True)
    command = (
        f"{settings['ekn']} _applyManifest {settings['manifests'][generation]}"
        f" --environment {environment}"
        f" --resource-priority-file {settings['resourcePriority']}"
    )
    if unit is not None:
        command += f" --unit {unit}"
    rc, out = await cp.execute(command, timeout=APPLY_TIMEOUT)
    if rc != 0:
        raise MachineError(f"[cp] {generation} did not apply (exit {rc}):\n{out}")
    print(out, flush=True)
    return out


async def inventory(cp, environment, unit=None):
    """Everything on the cluster inside one prune scope.

    Maps ``(kind, namespace, name)`` to the object's resourceVersion, over
    every kind any generation here applies.  The label selector is the same
    one `apply_and_prune` prunes by, so this sees exactly what a prune sees.
    """
    found = {}
    for resource in RESOURCES:
        data = await get_json(
            cp,
            f"get {resource} --all-namespaces --selector '{selector(environment, unit)}'",
        )
        for item in data["items"]:
            metadata = item["metadata"]
            key = (item["kind"], metadata.get("namespace", "none"), metadata["name"])
            found[key] = metadata["resourceVersion"]
    return found


def keys(inventoried):
    return sorted(f"{kind}/{namespace}/{name}" for kind, namespace, name in inventoried)


def expect(what, got, want):
    if got != want:
        raise MachineError(f"{what}\n  wanted: {want}\n  got:    {got}")


async def check_first_apply(cp, settings):
    """The whole generation lands, and the custom resource comes back.

    Reading the CR back is the point.  An apply that exits 0 says the API
    server accepted a PATCH; it does not say the object is there, that the
    environment label reached it, or that its own kind is being served
    under the plural its CRD declares.
    """
    await apply(cp, settings, "gen1", settings["environment"])

    namespace = settings["namespace"]
    alpha = await get_json(cp, f"get widget-parts alpha --namespace {namespace}")
    expect("the custom resource's spec", alpha["spec"], {"size": 1})
    expect(
        "the environment label on the custom resource",
        alpha["metadata"]["labels"].get(ENV_LABEL),
        settings["environment"],
    )

    expect(
        "what gen1 put on the cluster",
        keys(await inventory(cp, settings["environment"])),
        keys(
            {
                ("Namespace", "none", namespace),
                ("ConfigMap", namespace, "settings"),
                ("ConfigMap", namespace, "stale"),
                ("CustomResourceDefinition", "none", "widget-parts.ekn.example.com"),
                ("Deployment", namespace, "probe"),
                ("WidgetPart", namespace, "alpha"),
                ("WidgetPart", namespace, "beta"),
            }
        ),
    )


async def check_workload(cp, settings):
    """The Deployment's Pod runs, so a kubelet acted on the apply."""
    await wait_for_pods(cp, "--selector app=probe", namespace=settings["namespace"])
    print("[kubeapply] the workload is Ready", flush=True)


async def check_other_environment(cp, settings):
    """A second easykubenix instance, in a namespace of its own.

    It shares the custom kind with the generations above, and every prune
    after this has to leave it alone.  The label is the prune scope, and a
    prune that scanned by kind alone would take this out.
    """
    await apply(cp, settings, "other", settings["otherEnvironment"])
    expect(
        "what the other environment owns",
        keys(await inventory(cp, settings["otherEnvironment"])),
        keys(
            {
                ("Namespace", "none", settings["otherNamespace"]),
                ("WidgetPart", settings["otherNamespace"], "gamma"),
            }
        ),
    )


async def check_unit_apply(cp, settings):
    """A deployment unit of the *same* environment, in the same namespace.

    The unit label is rendered into the manifest, so `ekn` never writes it --
    it only reads it back to scope a prune.  This asserts both halves of that
    separation: the unit's objects answer the unit's selector, and they do not
    answer the whole-instance one, which excludes anything carrying the label.
    """
    namespace = settings["namespace"]
    environment, unit = settings["environment"], settings["unit"]
    await apply(cp, settings, "unit", environment, unit)

    expect(
        "what the unit owns",
        keys(await inventory(cp, environment, unit)),
        keys(
            {
                ("ConfigMap", namespace, "bootstrap"),
                ("ConfigMap", namespace, "bootstrap-extra"),
            }
        ),
    )

    instance = keys(await inventory(cp, environment))
    for name in (f"ConfigMap/{namespace}/bootstrap", f"ConfigMap/{namespace}/bootstrap-extra"):
        if name in instance:
            raise MachineError(f"{name} answers the whole-instance selector, which excludes unit objects")


async def check_unit_prune(cp, settings):
    """A unit's own prune deletes inside its scope and nowhere else.

    `unitReduced` drops `bootstrap-extra`, so this proves the unit scope is
    live rather than merely narrow -- a selector that matched nothing would
    pass the "leaves the instance alone" half on its own.
    """
    namespace = settings["namespace"]
    environment, unit = settings["environment"], settings["unit"]
    await apply(cp, settings, "unitReduced", environment, unit)

    expect(
        "what survives the unit's own prune",
        keys(await inventory(cp, environment, unit)),
        keys({("ConfigMap", namespace, "bootstrap")}),
    )

    expect(
        "the instance after the unit's prune",
        keys(await inventory(cp, environment)),
        keys(
            {
                ("Namespace", "none", namespace),
                ("ConfigMap", namespace, "settings"),
                ("CustomResourceDefinition", "none", "widget-parts.ekn.example.com"),
                ("Deployment", namespace, "probe"),
                ("WidgetPart", namespace, "alpha"),
            }
        ),
    )


async def check_idempotent(cp, settings):
    """The same generation applied twice changes nothing.

    Compared by resourceVersion, which the API server bumps on any write --
    so an apply that rewrote an unchanged object would show up here even
    though the object still reads the same.

    Only the two inert kinds.  A Deployment's resourceVersion moves whenever
    its controller updates the status, and a Namespace's when its finalizers
    settle, so neither says anything about what the apply did.

    Nothing here reads the apply's own output.  Server-side apply reports
    taking a field from another manager as a warning, which is a normal
    thing for a second apply to say and not a failure.
    """
    inert = {"ConfigMap", "WidgetPart"}

    def only_inert(inventoried):
        return {key: value for key, value in inventoried.items() if key[0] in inert}

    before = only_inert(await inventory(cp, settings["environment"]))
    await apply(cp, settings, "gen1", settings["environment"])
    after = only_inert(await inventory(cp, settings["environment"]))

    expect("a second apply of gen1 rewrote objects", after, before)
    print(f"[kubeapply] {len(before)} objects unchanged by a second apply", flush=True)


async def check_prune(cp, settings):
    """gen2 drops one ConfigMap and one WidgetPart, and only those go.

    The WidgetPart is the half worth having.  Pruning lists a kind back
    through the class the apply built for it, and a custom kind only gets a
    class from `ekn.apply.discover`; the builtin ConfigMap would pass with
    that path broken.
    """
    namespace = settings["namespace"]
    await apply(cp, settings, "gen2", settings["environment"])

    expect(
        "what survives gen2",
        keys(await inventory(cp, settings["environment"])),
        keys(
            {
                ("Namespace", "none", namespace),
                ("ConfigMap", namespace, "settings"),
                ("CustomResourceDefinition", "none", "widget-parts.ekn.example.com"),
                ("Deployment", namespace, "probe"),
                ("WidgetPart", namespace, "alpha"),
            }
        ),
    )

    expect(
        "the other environment after a prune",
        keys(await inventory(cp, settings["otherEnvironment"])),
        keys(
            {
                ("Namespace", "none", settings["otherNamespace"]),
                ("WidgetPart", settings["otherNamespace"], "gamma"),
            }
        ),
    )

    # The load-bearing half of the whole-instance selector.  gen2 shares this
    # environment and this namespace with the unit, and applies neither of the
    # unit's ConfigMaps -- so without the `!ekn.dev/deployment-unit` clause it
    # would list them, find them absent from its desired set, and delete them.
    # On a real cluster that is ArgoCD and the CNI.
    expect(
        "the deployment unit after a whole-instance prune",
        keys(await inventory(cp, settings["environment"], settings["unit"])),
        keys(
            {
                ("ConfigMap", namespace, "bootstrap"),
                ("ConfigMap", namespace, "bootstrap-extra"),
            }
        ),
    )


async def check_prune_gap(cp, settings):
    """The documented limitation, as it behaves rather than as it should.

    `apply_and_prune` scans only the kinds the current apply touches, so a
    generation that drops the *last* object of a kind leaves it behind: gen3
    has no WidgetPart at all, so nothing looks for a stale one, and `alpha`
    survives.  Its docstring says so.

    Written down here because a gap nobody can see is a gap nobody fixes.
    When `apply_and_prune` takes a kind list independent of the apply set,
    this test fails, and the fix is to move `alpha` out of `wanted` -- not to
    delete the test.
    """
    namespace = settings["namespace"]
    await apply(cp, settings, "gen3", settings["environment"])

    expect(
        "what survives gen3 -- see this test's docstring, which explains why"
        " the wanted list below still holds a WidgetPart",
        keys(await inventory(cp, settings["environment"])),
        keys(
            {
                ("Namespace", "none", namespace),
                ("ConfigMap", namespace, "settings"),
                ("CustomResourceDefinition", "none", "widget-parts.ekn.example.com"),
                ("Deployment", namespace, "probe"),
                # The gap: gen3 generates no WidgetPart, so the prune scan
                # never asks about that kind.
                ("WidgetPart", namespace, "alpha"),
            }
        ),
    )
    print("[kubeapply] the prune gap still behaves as documented", flush=True)


async def test(vms):
    settings = vms.settings
    print(
        f"[kubeapply] kubernetes {settings['kubernetesVersion']}, environment {settings['environment']}",
        flush=True,
    )

    cp = await bring_up(vms)

    await check_first_apply(cp, settings)
    await check_workload(cp, settings)
    await check_other_environment(cp, settings)
    await check_unit_apply(cp, settings)
    await check_idempotent(cp, settings)
    await check_prune(cp, settings)
    await check_unit_prune(cp, settings)
    await check_prune_gap(cp, settings)

    print(
        "[kubeapply] " + await kubectl(cp, "get all --all-namespaces"),
        flush=True,
    )


run_test(test)
