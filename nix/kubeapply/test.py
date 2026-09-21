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
    fast mode      --assume-unchanged skips what nothing has written, and
                   sends again exactly what somebody did write
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

import re
from typing import Any

from uml_runner import Machine, MachineError, Machines, run_test
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


def selector(environment: str, unit: str | None = None, hand_applied: tuple[str, ...] = ()) -> str:
    """The label selector one apply owns.  Mirrors `apply.prune_selector`.

    Written out again here rather than imported, deliberately.  A test that
    asks the code under test what it prunes by cannot catch that answer being
    wrong; this one asks the API server the same question independently.

    The `notin` clause is also the one behaviour a double cannot check: that
    it matches an object carrying no unit label at all.  Only a real API
    server answers that, and if it did not, the whole-instance scope would
    silently shrink to nothing while every assertion still passed.
    """
    if unit is not None:
        return f"{ENV_LABEL}={environment},{UNIT_LABEL}={unit}"
    if not hand_applied:
        return f"{ENV_LABEL}={environment}"
    return f"{ENV_LABEL}={environment},{UNIT_LABEL} notin ({','.join(sorted(hand_applied))})"


def hand_applied_units(settings: dict[str, Any], unit: str | None) -> tuple[str, ...]:
    """The units a prune of this scope must not touch.

    Derived in one place rather than at each call, because a site that forgot
    it would widen the scope silently -- and the assertion it would break is
    "the whole-instance prune left the unit alone", which is the one this
    file exists for.  `settings["unit"]` is a `deployment.units.<name>` with
    its own `modules` (see ../kubeapply/manifests.nix), so it is exactly the
    hand-applied case.  A `--target` prune excludes nothing.
    """
    return () if unit is not None else (settings["unit"],)


async def apply(  # noqa: PLR0913 -- one call site per check, and each argument is a flag the command takes
    cp: Machine,
    settings: dict[str, Any],
    generation: str,
    environment: str,
    unit: str | None = None,
    assume_unchanged: bool = False,
) -> str:
    """Run one `ekn _applyManifest` in the guest, and return its output.

    The output is printed whether the apply passes or fails.  `ekn` logs a
    line per barrier and a line per pruned object, and that is the record of
    what the apply did -- an hour-long gate whose build log says only
    "passed" answers nothing when somebody asks it later.
    """
    hand_applied = hand_applied_units(settings, unit)
    scope = environment if unit is None else f"{environment}/{unit}"
    print(f"[kubeapply] applying {generation} as {scope}", flush=True)
    command = (
        f"{settings['ekn']} _applyManifest {settings['manifests'][generation]}"
        f" --environment {environment}"
        f" --resource-priority-file {settings['resourcePriority']}"
    )
    if assume_unchanged:
        command += " --assume-unchanged"
    if unit is not None:
        command += f" --unit {unit}"
    for excluded in hand_applied:
        command += f" --hand-applied {excluded}"
    rc, out = await cp.execute(command, timeout=APPLY_TIMEOUT)
    if rc != 0:
        raise MachineError(f"[cp] {generation} did not apply (exit {rc}):\n{out}")
    print(out, flush=True)
    return out


async def inventory(
    cp: Machine, settings: dict[str, Any], environment: str, unit: str | None = None
) -> dict[tuple[str, str, str], str]:
    """Everything on the cluster inside one prune scope.

    Maps ``(kind, namespace, name)`` to the object's resourceVersion, over
    every kind any generation here applies.  The label selector is the same
    one `apply_and_prune` prunes by, so this sees exactly what a prune sees.
    """
    scope = selector(environment, unit, hand_applied_units(settings, unit))
    found = {}
    for resource in RESOURCES:
        data = await get_json(
            cp,
            f"get {resource} --all-namespaces --selector '{scope}'",
        )
        for item in data["items"]:
            metadata = item["metadata"]
            key = (item["kind"], metadata.get("namespace", "none"), metadata["name"])
            found[key] = metadata["resourceVersion"]
    return found


def keys(inventoried: dict[tuple[str, str, str], str] | set[tuple[str, str, str]]) -> list[str]:
    return sorted(f"{kind}/{namespace}/{name}" for kind, namespace, name in inventoried)


def expect(what: str, got: object, want: object) -> None:
    if got != want:
        raise MachineError(f"{what}\n  wanted: {want}\n  got:    {got}")


async def check_first_apply(cp: Machine, settings: dict[str, Any]) -> None:
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
        keys(await inventory(cp, settings, settings["environment"])),
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


async def check_workload(cp: Machine, settings: dict[str, Any]) -> None:
    """The Deployment's Pod runs, so a kubelet acted on the apply."""
    await wait_for_pods(cp, "--selector app=probe", namespace=settings["namespace"])
    print("[kubeapply] the workload is Ready", flush=True)


async def check_other_environment(cp: Machine, settings: dict[str, Any]) -> None:
    """A second easykubenix instance, in a namespace of its own.

    It shares the custom kind with the generations above, and every prune
    after this has to leave it alone.  The label is the prune scope, and a
    prune that scanned by kind alone would take this out.
    """
    await apply(cp, settings, "other", settings["otherEnvironment"])
    expect(
        "what the other environment owns",
        keys(await inventory(cp, settings, settings["otherEnvironment"])),
        keys(
            {
                ("Namespace", "none", settings["otherNamespace"]),
                ("WidgetPart", settings["otherNamespace"], "gamma"),
            }
        ),
    )


async def check_unit_apply(cp: Machine, settings: dict[str, Any]) -> None:
    """A deployment unit of the *same* environment, in the same namespace.

    The unit label is rendered into the manifest, so `ekn` never writes it --
    it only reads it back to scope a prune.  This asserts both halves of that
    separation: the unit's objects answer the unit's selector, and they do not
    answer the whole-instance one, which excludes this unit by name.
    """
    namespace = settings["namespace"]
    environment, unit = settings["environment"], settings["unit"]
    await apply(cp, settings, "unit", environment, unit)

    expect(
        "what the unit owns",
        keys(await inventory(cp, settings, environment, unit)),
        keys(
            {
                ("ConfigMap", namespace, "bootstrap"),
                ("ConfigMap", namespace, "bootstrap-extra"),
            }
        ),
    )

    instance = keys(await inventory(cp, settings, environment))
    for name in (f"ConfigMap/{namespace}/bootstrap", f"ConfigMap/{namespace}/bootstrap-extra"):
        if name in instance:
            raise MachineError(f"{name} answers the whole-instance selector, which excludes unit objects")


async def check_unit_prune(cp: Machine, settings: dict[str, Any]) -> None:
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
        keys(await inventory(cp, settings, environment, unit)),
        keys({("ConfigMap", namespace, "bootstrap")}),
    )

    expect(
        "the instance after the unit's prune",
        keys(await inventory(cp, settings, environment)),
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


async def check_idempotent(cp: Machine, settings: dict[str, Any]) -> None:
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

    def only_inert(inventoried: dict[tuple[str, str, str], str]) -> dict[tuple[str, str, str], str]:
        return {key: value for key, value in inventoried.items() if key[0] in inert}

    before = only_inert(await inventory(cp, settings, settings["environment"]))
    await apply(cp, settings, "gen1", settings["environment"])
    after = only_inert(await inventory(cp, settings, settings["environment"]))

    expect("a second apply of gen1 rewrote objects", after, before)
    print(f"[kubeapply] {len(before)} objects unchanged by a second apply", flush=True)


def cache_report(output: str) -> dict[str, int]:
    """The `apply cache skipped=N recorded=M cold=K` line, as a dict of ints.

    Read from the log rather than from a file, because the file is the thing
    under test: a run that wrote the cache and skipped nothing, and a run that
    skipped everything, leave the same file.

    The escapes go first.  structlog's ConsoleRenderer colours every key and
    every value whether or not a terminal is attached, so a colour reset sits
    between the key and its `=`: the event name still matches, and no
    `field=value` does.
    """
    for line in re.sub(r"\x1b\[[0-9;]*m", "", output).splitlines():
        if "apply cache" not in line:
            continue
        found = {}
        for field in ("skipped", "recorded", "cold"):
            marker = f"{field}="
            if marker in line:
                found[field] = int(line.split(marker, 1)[1].split()[0].strip("'\""))
        if found:
            return found
    raise MachineError(f"no 'apply cache' line in:\n{output}")


async def check_assume_unchanged(cp: Machine, settings: dict[str, Any]) -> None:
    """`--assume-unchanged` against a real API server: both routes to a skip.

    Nothing else here can answer this.  The unit tests describe the gate
    against a double this repository wrote, and what it rests on belongs to
    the API server: that a `PartialObjectMetadataList` LIST at
    `resourceVersion=0` answers with every object's metadata, and that a
    server-side apply which changes nothing does not move a
    `resourceVersion`.

    Four runs, because there are two ways to decide a skip and they have to
    be told apart:

    1. **cold** -- nothing recorded on this machine.  Earlier checks applied
       gen1 through an ordinary apply, so the objects are on the cluster
       carrying the hash the render stamped and this environment's label.
       Every skip here is decided by that, and `cold=` counts them.
    2. **recorded** -- the cold run wrote down where it found each object, so
       this run decides on the `resourceVersion` instead and `cold=` is zero.
       That is what makes the write below visible at all.
    3. **written** -- somebody edits one object, and exactly that object is
       sent again.
    """
    environment = settings["environment"]
    namespace = settings["namespace"]

    cold = cache_report(await apply(cp, settings, "gen1", environment, assume_unchanged=True))
    if cold["skipped"] == 0:
        raise MachineError(
            "the first --assume-unchanged run skipped nothing. The render stamps every object with "
            "ekn.dev/manifest-hash and these were applied earlier in this test, so a cold run has "
            "everything it needs: either the sweep read no metadata, or the hash disagrees with the render."
        )
    expect("skips decided by the rendered hash", cold["cold"], cold["skipped"])
    expect("a cold run sent anything", cold["recorded"], 0)

    warm = cache_report(await apply(cp, settings, "gen1", environment, assume_unchanged=True))
    expect("the second run skipped a different number", warm["skipped"], cold["skipped"])
    # The whole of why a skip records where it looked. Without it this run
    # would take the cold route again, and a write under a manager that route
    # allows would never be seen.
    expect("skips still decided by the rendered hash", warm["cold"], 0)
    settled = await inventory(cp, settings, environment)

    # Somebody else writes one object.  Only its resourceVersion moves -- the
    # bytes `ekn` sent are still what this machine recorded, and an annotation
    # nobody declares survives a server-side apply -- so that version is the
    # only thing left that can tell the next run to send it.
    await kubectl(cp, f"annotate configmap settings --namespace {namespace} kubeapply.test/edited=yes")
    moved = {key for key, version in (await inventory(cp, settings, environment)).items() if settled[key] != version}

    edited = cache_report(await apply(cp, settings, "gen1", environment, assume_unchanged=True))

    # Measured against what actually moved, not against a count of one: the
    # Deployment's own controller writes its status, and a run that skipped
    # one object fewer because of that would otherwise read as this working.
    expect("what the edit moved", ("ConfigMap", namespace, "settings") in moved, True)
    expect("objects sent again", warm["skipped"] - edited["skipped"], len(moved))
    expect("objects recorded again", edited["recorded"], len(moved))
    print(
        f"[kubeapply] {cold['cold']} skipped cold, {edited['skipped']} skipped on record, "
        f"{len(moved)} written since and applied again",
        flush=True,
    )


async def check_prune(cp: Machine, settings: dict[str, Any]) -> None:
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
        keys(await inventory(cp, settings, settings["environment"])),
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
        keys(await inventory(cp, settings, settings["otherEnvironment"])),
        keys(
            {
                ("Namespace", "none", settings["otherNamespace"]),
                ("WidgetPart", settings["otherNamespace"], "gamma"),
            }
        ),
    )

    # The load-bearing half of the whole-instance selector.  gen2 shares this
    # environment and this namespace with the unit, and applies neither of the
    # unit's ConfigMaps -- so without the `notin` exclusion it would list them,
    # find them absent from its desired set, and delete them.  On a real
    # cluster that is ArgoCD and the CNI.
    expect(
        "the deployment unit after a whole-instance prune",
        keys(await inventory(cp, settings, settings["environment"], settings["unit"])),
        keys(
            {
                ("ConfigMap", namespace, "bootstrap"),
                ("ConfigMap", namespace, "bootstrap-extra"),
            }
        ),
    )


async def check_prune_gap(cp: Machine, settings: dict[str, Any]) -> None:
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
        keys(await inventory(cp, settings, settings["environment"])),
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


async def test(vms: Machines) -> None:
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
    await check_assume_unchanged(cp, settings)
    await check_prune(cp, settings)
    await check_unit_prune(cp, settings)
    await check_prune_gap(cp, settings)

    print(
        "[kubeapply] " + await kubectl(cp, "get all --all-namespaces"),
        flush=True,
    )


run_test(test)
