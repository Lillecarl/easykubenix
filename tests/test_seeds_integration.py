"""The seeded-credential lifecycle, against a real kube-apiserver.

`tests/test_seeds.py` covers the same four cases with a fake cluster, which
is where the logic is pinned down. This file exists for the parts a fake
cannot answer, and one of them is the whole design:

**Server-side apply deletes fields the applying manager previously owned and
then omits.** `ekn` applies with `force=true` under its own field manager, so
"leave this seed alone" cannot mean applying the rendered object -- that
overwrites the live credential with the literal `$ekn:env:VARNAME` on the
second apply -- and cannot mean omitting the field either, which deletes it.
It means writing the value the cluster already holds back into the field, so
every other field of the object still reconciles. Both wrong versions look
correct in a unit test. Only a real API server has the field-management
machinery that makes them wrong, so only a real API server can catch them.

The walk is deliberately one ordered test rather than four independent ones.
The cases are a lifecycle -- create, then leave alone, then rotate -- and the
interesting failures are in the transitions.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import kr8s
import kr8s.asyncio
import pytest
from anyio import Path as AnyioPath

from ekn import seeds
from ekn.apply import apply_and_prune, build_object
from ekn.eval import evaluate_validation_file
from ekn.validation import EphemeralControlPlane

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = PROJECT_ROOT / "tests/seed_integration.nix"

VARIABLE = "EKN_TEST_SEED_PASSWORD"
SECRET = {
    "apiVersion": "v1",
    "kind": "Secret",
    "metadata": {"name": "repo-creds", "namespace": "default"},
}


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="module")
async def cluster() -> AsyncIterator[tuple[Any, list[dict[str, Any]], str]]:
    """A booted control plane, the rendered objects, and the environment.

    Module-scoped: booting etcd and an API server costs far more than every
    assertion in this file put together. `EphemeralControlPlane` mutates
    `os.environ` (KUBECONFIG and friends) and does not restore it, which is
    documented behaviour rather than a surprise -- nothing else in the suite
    talks to a cluster.
    """
    cfg = await evaluate_validation_file(FIXTURE, None)
    c = cfg.config
    objects = json.loads(await AnyioPath(c.internal.manifest_json_file.out_path).read_text())
    if isinstance(objects, dict):
        objects = objects["items"]

    async with EphemeralControlPlane(
        k8s_bin=c.kubernetes.package.out_path + "/bin",
        etcd_bin=c.validation.etcd_package.out_path + "/bin",
        kubeconform_bin=c.validation.kubeconform_package.out_path + "/bin",
        service_subnet=c.validation.service_subnet,
        kubeadm_config=c.validation.kubeadm_config,
    ) as plane:
        api = await kr8s.asyncio.api(kubeconfig=plane.kubeconfig)
        yield api, objects, c.ekn.environment


async def live_password(api: Any) -> str | None:
    """The password actually stored in the cluster, or None if absent."""
    obj = await build_object(dict(SECRET), api)
    try:
        await obj.async_refresh()
    except kr8s.NotFoundError:
        return None
    data = dict(obj.raw).get("data") or {}
    encoded = data.get("password")
    return base64.b64decode(encoded).decode() if isinstance(encoded, str) else None


async def apply(api: Any, objects: list[dict[str, Any]], environment: str) -> list[Any]:
    """What `ekn kubeapply` does: resolve seeds, then apply what survives."""
    plan = await seeds.resolve(objects, api=api)
    await apply_and_prune(plan.objects, api=api, environment=environment, prune=False)
    return plan.actions


async def test_the_rendered_manifest_carries_a_reference_not_a_value(
    cluster: tuple[Any, list[dict[str, Any]], str],
) -> None:
    """Nix never sees a secret, so the manifest holds the reference."""
    _, objects, _ = cluster
    secret = next(o for o in objects if o["kind"] == "Secret")
    assert secret["stringData"]["password"] == f"$ekn:env:{VARIABLE}"
    assert secret["metadata"]["annotations"]["ekn.dev/env-0"] == VARIABLE
    # The manifest is schema-valid Kubernetes: a plain string, not an object.
    # This is what lets `ekn validate` and kubeconform work untouched.
    assert isinstance(secret["stringData"]["password"], str)


async def test_the_seeded_credential_lifecycle(
    cluster: tuple[Any, list[dict[str, Any]], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, objects, environment = cluster

    # --- not in cluster, variable unset: abort, and create nothing ---------
    monkeypatch.delenv(VARIABLE, raising=False)
    with pytest.raises(seeds.MissingVariablesError, match=VARIABLE):
        await apply(api, objects, environment)
    assert await live_password(api) is None, "the abort must happen before anything is applied"

    # --- not in cluster, variable set: create with the real value ----------
    monkeypatch.setenv(VARIABLE, "first-secret")
    actions = await apply(api, objects, environment)
    assert [a.verb for a in actions] == ["create"]
    assert await live_password(api) == "first-secret"

    # --- in cluster, variable unset: skip, and DO NOT clobber --------------
    # The steady state, and the case this file exists for. A seed is exported
    # once and dropped, so every later apply lands here. If "skip" were
    # implemented as applying the rendered object, server-side apply would
    # replace the stored password with the literal reference.
    monkeypatch.delenv(VARIABLE, raising=False)
    actions = await apply(api, objects, environment)
    assert [a.verb for a in actions] == ["skip"]
    assert await live_password(api) == "first-secret", "a second apply with the variable unset overwrote the credential"

    # A third apply, because a field manager's ownership only becomes
    # interesting once it has applied more than once.
    actions = await apply(api, objects, environment)
    assert [a.verb for a in actions] == ["skip"]
    assert await live_password(api) == "first-secret"

    # --- in cluster, variable set to the same value: no-op -----------------
    monkeypatch.setenv(VARIABLE, "first-secret")
    actions = await apply(api, objects, environment)
    assert [a.verb for a in actions] == ["unchanged"]
    assert await live_password(api) == "first-secret"

    # --- in cluster, variable set to a different value: update -------------
    monkeypatch.setenv(VARIABLE, "second-secret")
    actions = await apply(api, objects, environment)
    assert [a.verb for a in actions] == ["update"]
    assert await live_password(api) == "second-secret"

    # --- and back to the steady state, on the rotated value ----------------
    monkeypatch.delenv(VARIABLE, raising=False)
    actions = await apply(api, objects, environment)
    assert [a.verb for a in actions] == ["skip"]
    assert await live_password(api) == "second-secret"


async def test_the_unseeded_objects_are_applied_normally(
    cluster: tuple[Any, list[dict[str, Any]], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A seed in the set must not hold up everything else.

    The abort is deliberately before any apply, so this runs with the
    variable set -- the point is that the ConfigMap beside the Secret lands
    normally rather than being caught up in seed handling.
    """
    api, objects, environment = cluster
    monkeypatch.setenv(VARIABLE, "second-secret")
    await apply(api, objects, environment)

    config_map = await build_object(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "plain", "namespace": "default"},
        },
        api,
    )
    await config_map.async_refresh()
    assert dict(config_map.raw)["data"] == {"key": "value"}


async def test_nothing_reaches_the_cluster_holding_a_reference(
    cluster: tuple[Any, list[dict[str, Any]], str],
) -> None:
    """The failure this whole mechanism exists to prevent.

    Whatever the path taken above, no object in the cluster may hold a
    literal `$ekn:env:` anywhere -- that is what a missed substitution, or a
    skip implemented as an apply, would leave behind.
    """
    api, _, _ = cluster
    found: list[str] = []
    async for obj in api.async_get("secrets", namespace="default"):
        raw = json.dumps(dict(obj.raw))
        decoded = json.dumps(
            {
                key: base64.b64decode(value).decode(errors="replace")
                for key, value in (dict(obj.raw).get("data") or {}).items()
                if isinstance(value, str)
            }
        )
        if seeds.REFERENCE_PREFIX in raw or seeds.REFERENCE_PREFIX in decoded:
            found.append(str(obj.name))
    assert found == [], f"objects reached the cluster holding an unresolved reference: {found}"


async def test_the_environment_is_the_only_source_of_the_value() -> None:
    """The rendered manifest is in the Nix store, so it must never hold one.

    `ekn.cacheTo` pushes that closure to a binary cache, and the store is
    world-readable. This is the property the design is built around, so it
    is asserted rather than assumed.
    """
    cfg = await evaluate_validation_file(FIXTURE, None)
    manifest_path = cfg.config.internal.manifest_json_file.out_path
    assert manifest_path.startswith("/nix/store/")
    contents = await AnyioPath(manifest_path).read_text()
    assert "first-secret" not in contents
    assert "second-secret" not in contents
    assert os.environ.get(VARIABLE) is None or os.environ[VARIABLE] not in contents
    assert f"$ekn:env:{VARIABLE}" in contents


async def test_an_empty_variable_does_not_count_as_supplied(
    cluster: tuple[Any, list[dict[str, Any]], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exported-but-empty variable must abort, not create an empty secret.

    Found in use: a token extraction produced nothing, the shell exported
    the empty result, and the apply succeeded with a zero-length password.
    That is worse than the unset case, because it looks like success until
    the consumer fails to authenticate.
    """
    api, objects, environment = cluster
    secret = await build_object(dict(SECRET), api)
    try:
        await secret.async_refresh()
        await secret.delete()
    except kr8s.NotFoundError:
        pass

    monkeypatch.setenv(VARIABLE, "")
    with pytest.raises(seeds.MissingVariablesError, match=VARIABLE):
        await apply(api, objects, environment)
    assert await live_password(api) is None


async def test_other_fields_reconcile_while_the_credential_is_left_alone(
    cluster: tuple[Any, list[dict[str, Any]], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A seeded Secret is ordinary configuration with a credential in one
    field, so editing `username` must land even when the password is being
    left alone.

    An earlier version dropped the whole object on a skip, which froze every
    other field until the password itself changed. Found in use, on a
    `username` edit that silently did not apply.
    """
    api, objects, environment = cluster

    monkeypatch.setenv(VARIABLE, "lifecycle-secret")
    await apply(api, objects, environment)
    assert await live_password(api) == "lifecycle-secret"

    # Edit a non-secret field, and drop the variable -- the steady state.
    edited = [
        {**o, "stringData": {**o["stringData"], "username": "oauth2"}} if o["kind"] == "Secret" else o for o in objects
    ]
    monkeypatch.delenv(VARIABLE, raising=False)
    actions = await apply(api, edited, environment)

    assert [a.verb for a in actions] == ["skip"]
    secret = await build_object(dict(SECRET), api)
    await secret.async_refresh()
    stored = {key: base64.b64decode(value).decode() for key, value in (dict(secret.raw).get("data") or {}).items()}
    assert stored["username"] == "oauth2", "an unrelated field change did not reconcile"
    assert stored["password"] == "lifecycle-secret", "the credential was not left alone"


async def test_prune_does_not_delete_a_seed_it_could_not_produce(
    cluster: tuple[Any, list[dict[str, Any]], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one branch that still drops a seeded object from the apply set.

    `resolve` normally writes the live credential back, so a skipped seed is
    in the desired set and prune leaves it alone. It cannot do that when the
    live value will not read back -- a non-UTF-8 value, or the key already
    gone. The object then leaves the apply set entirely.

    Absence from the desired set otherwise means "removed from the
    configuration", and prune answers that with a delete. Here it means "this
    run had nothing safe to write", and a delete destroys a credential nobody
    can recreate: the variable was exported once, at bootstrap, and is long
    gone from the environment.

    `prune_kinds` forces the Secret scan. Without it the documented
    current-apply-only scan would skip Secrets entirely once the only Secret
    left the apply set, and the test would pass without exercising anything.
    """
    api, objects, environment = cluster

    monkeypatch.setenv(VARIABLE, "unrecoverable")
    await apply(api, objects, environment)
    assert await live_password(api) == "unrecoverable"

    # Delete the key from the live object. `_with_live_values` now has
    # nothing to read back, which is what drops the object from the plan.
    secret = await build_object(dict(SECRET), api)
    await secret.async_refresh()
    await secret.async_patch({"data": {"password": None}})
    assert await live_password(api) is None

    monkeypatch.delenv(VARIABLE, raising=False)
    plan = await seeds.resolve(objects, api=api)
    names = [o["metadata"]["name"] for o in plan.objects]
    assert "repo-creds" not in names, "fixture no longer exercises the drop branch"

    await apply_and_prune(
        plan.objects,
        api=api,
        environment=environment,
        prune=True,
        prune_kinds={"Secret"},
        protect=plan.protected,
    )

    # Still there. Without `protect` this delete is what takes the credential.
    survivor = await build_object(dict(SECRET), api)
    await survivor.async_refresh()
    assert survivor.name == "repo-creds"
