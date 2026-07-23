from __future__ import annotations

import os
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from os import PathLike
from typing import Any

from nanopynix import NixError, NixEvalSettings, NixSettings, Session
from nanopynix.primops import yaml_primops

_SESSION_SETTINGS = NixSettings()


def _profiler_eval_settings() -> NixEvalSettings | None:
    """Build eval-profiler settings from EKN_EVAL_PROFILER* env vars, if set.

    Unset by default so normal runs are unaffected. Set EKN_EVAL_PROFILER=
    flamegraph (plus optionally EKN_EVAL_PROFILE_FILE and
    EKN_EVAL_PROFILER_FREQUENCY) to profile the exact same code path a real
    `ekn eval`/`ekn render` invocation takes.
    """
    profiler = os.environ.get("EKN_EVAL_PROFILER")
    if not profiler:
        return None
    return NixEvalSettings(
        eval_profiler=profiler,
        eval_profile_file=os.environ.get("EKN_EVAL_PROFILE_FILE", "nix.profile"),
        eval_profiler_frequency=int(os.environ.get("EKN_EVAL_PROFILER_FREQUENCY", "0")),
    )


@asynccontextmanager
async def _session() -> AsyncIterator[Session]:
    async with Session(
        settings=_SESSION_SETTINGS,
        verbosity="error",
        # yaml_primops() (fromYAML/fromYAML11/*Stream/toYAML) are bundled
        # with nanopynix but opt-in, not auto-registered by Session -- needed
        # so Nix-side chart-rendering code (renderChart.nix) can parse
        # `helm template`'s IFD-built output in-process via fromYAML11Stream.
        primops=yaml_primops(),
    ) as session:
        yield session


async def evaluate_file(file: str | PathLike[str], attr_path: str | None) -> object:
    async with (
        _session() as session,
        session.store() as store,
        session.eval(store, eval_settings=_profiler_eval_settings()) as eval_,
    ):
        root = await (await eval_.file(str(file))).auto_call()

        proxy = root
        if attr_path:
            for name in attr_path.split("."):
                if not name:
                    raise ValueError(f"empty segment in attr path: {attr_path!r}")
                proxy = proxy.attr(name)

        return await proxy.force_json()


async def evaluate_file_multi(
    file: str | PathLike[str],
    *attr_paths: str | None,
) -> list[object]:
    results: list[object] = []
    async with (
        _session() as session,
        session.store() as store,
        session.eval(store, eval_settings=_profiler_eval_settings()) as eval_,
    ):
        root = await (await eval_.file(str(file))).auto_call()
        for attr_path in attr_paths:
            proxy = root
            if attr_path:
                for name in attr_path.split("."):
                    if not name:
                        raise ValueError(
                            f"empty segment in attr path: {attr_path!r}"
                        )
                    proxy = proxy.attr(name)
            results.append(await proxy.force_json())
    return results


async def evaluate_flake(flake_uri: str, attr_path: str | None) -> object:
    async with (
        _session() as session,
        session.store() as store,
        session.eval(store, eval_settings=_profiler_eval_settings()) as eval_,
    ):
        root = await eval_.eval_flake(flake_uri)

        proxy = root
        if attr_path:
            for name in attr_path.split("."):
                if not name:
                    raise ValueError(f"empty segment in attr path: {attr_path!r}")
                proxy = proxy.attr(name)

        return await proxy.force_json()


async def evaluate_flake_ekn(flake_uri: str, customer: str) -> dict:
    async with (
        _session() as session,
        session.store() as store,
        session.eval(store, eval_settings=_profiler_eval_settings()) as eval_,
    ):
        outputs = await eval_.eval_flake(flake_uri)
        system = await (await eval_.string("builtins.currentSystem")).force_json()
        proxy = outputs.attr("eknConfig").attr(str(system)).attr(customer)
        if await proxy.has_attr("config"):
            proxy = proxy.attr("config")

        generated = await proxy.attr("kubernetes").attr("generated").force_json()
        return {
            "config": {
                "kubernetes": {
                    "generated": generated,
                },
            }
        }


def _timing_enabled() -> bool:
    return bool(os.environ.get("EKN_TIMING"))


def _log_timing(label: str, elapsed: float) -> None:
    if _timing_enabled():
        print(f"[EKN_TIMING] {label}: {elapsed:.3f}s", file=sys.stderr)


async def evaluate_generated_manifests(
    file: str | PathLike[str] | None,
    flake_uri: str | None,
    customer: str | None,
    attr_path: str | None,
) -> Any:
    """Resolve a file or flake target down to `kubernetes.generated`.

    Unlike `evaluate_file`/`evaluate_flake`, this never force_json's the whole
    module `config` -- easykubenix options without a default (e.g. unset
    `gitops.branch`) would blow up a blanket deep evaluation even when unused.
    Uses `generated` (a flat list) rather than `generatedByPath`, which costs
    an extra O(n) chain of `lib.recursiveUpdate` calls in Nix just to
    pre-group by namespace/kind/name -- callers that need that grouping (e.g.
    GitOps routing) build the lookup themselves in Python instead.
    """
    t_start = time.monotonic()
    async with (
        _session() as session,
        session.store() as store,
        session.eval(store, eval_settings=_profiler_eval_settings()) as eval_,
    ):
        t_session_ready = time.monotonic()
        _log_timing("session/store/eval-session setup", t_session_ready - t_start)

        if flake_uri is not None:
            outputs = await eval_.eval_flake(flake_uri)
            if customer:
                system = await (await eval_.string("builtins.currentSystem")).force_json()
                proxy = outputs.attr("eknConfig").attr(str(system)).attr(customer)
            else:
                proxy = outputs
        elif file is not None:
            proxy = await (await eval_.file(str(file))).auto_call()
        else:
            raise ValueError("specify --file or --flake")

        if attr_path:
            for name in attr_path.split("."):
                if not name:
                    raise ValueError(f"empty segment in attr path: {attr_path!r}")
                proxy = proxy.attr(name)

        if await proxy.has_attr("config"):
            proxy = proxy.attr("config")

        t_before_force = time.monotonic()
        result = await proxy.attr("kubernetes").attr("generated").force_json()
        t_after_force = time.monotonic()
        _log_timing("force_json(kubernetes.generated)", t_after_force - t_before_force)
        _log_timing("total evaluate_generated_manifests", t_after_force - t_start)
        return result


async def evaluate_gitops_manifests(
    file: str | PathLike[str] | None,
    flake_uri: str | None,
    customer: str | None,
    attr_path: str | None,
) -> dict:
    """Resolve to `{"config": {"kubernetes": {"gitopsTargets": ...}}}`.

    Used by Diff/Commit/Deploy, which only ever read this field via
    `_gitops_file_groups`. Diff/Commit previously went through the generic
    `_evaluate` -> `evaluate_file`/`evaluate_flake`, which force_json's the
    *entire* narrowed `config` (every option in every module, not just
    kubernetes.gitopsTargets) before `_dig()`-ing this field out --
    forcing everything else was pure waste.
    """
    async with (
        _session() as session,
        session.store() as store,
        session.eval(store, eval_settings=_profiler_eval_settings()) as eval_,
    ):
        if flake_uri is not None:
            outputs = await eval_.eval_flake(flake_uri)
            if customer:
                system = await (await eval_.string("builtins.currentSystem")).force_json()
                proxy = outputs.attr("eknConfig").attr(str(system)).attr(customer)
            else:
                proxy = outputs
        elif file is not None:
            proxy = await (await eval_.file(str(file))).auto_call()
        else:
            raise ValueError("specify --file or --flake")

        if attr_path:
            for name in attr_path.split("."):
                if not name:
                    raise ValueError(f"empty segment in attr path: {attr_path!r}")
                proxy = proxy.attr(name)

        if await proxy.has_attr("config"):
            proxy = proxy.attr("config")

        gitops_targets = await proxy.attr("kubernetes").attr("gitopsTargets").force_json()
        return {
            "config": {
                "kubernetes": {
                    "gitopsTargets": gitops_targets,
                },
            }
        }


async def evaluate_kubeapply_config(
    file: str | PathLike[str] | None,
    flake_uri: str | None,
    customer: str | None,
    attr_path: str | None,
    target: str | None,
) -> dict[str, Any]:
    """Resolve the object list `ekn kubeapply` should apply, plus the
    `kluctl.discriminator`/`kluctl.resourcePriority` `apply_and_prune` needs
    and `kubernetes.sopsAgeIdentities` (SOPS age decrypt identities some
    consumer needs bootstrapped as a Secret -- see `ekn.sops.ensure_age_identities`).

    `target` narrows to one `kubernetes.gitopsTargets` entry's objects,
    `.ekn` routing metadata stripped; omitted, force_json's the full
    `kubernetes.generated` instead -- never both, so this only ever forces
    the one field it actually needs.
    """
    async with (
        _session() as session,
        session.store() as store,
        session.eval(store, eval_settings=_profiler_eval_settings()) as eval_,
    ):
        if flake_uri is not None:
            outputs = await eval_.eval_flake(flake_uri)
            if customer:
                system = await (await eval_.string("builtins.currentSystem")).force_json()
                proxy = outputs.attr("eknConfig").attr(str(system)).attr(customer)
            else:
                proxy = outputs
        elif file is not None:
            proxy = await (await eval_.file(str(file))).auto_call()
        else:
            raise ValueError("specify --file or --flake")

        if attr_path:
            for name in attr_path.split("."):
                if not name:
                    raise ValueError(f"empty segment in attr path: {attr_path!r}")
                proxy = proxy.attr(name)

        if await proxy.has_attr("config"):
            proxy = proxy.attr("config")

        if target:
            gitops_targets = await proxy.attr("kubernetes").attr("gitopsTargets").force_json()
            if not isinstance(gitops_targets, dict):
                raise ValueError("kubernetes.gitopsTargets did not evaluate to an object")
            resolved = gitops_targets.get(target)
            if not isinstance(resolved, dict):
                raise ValueError(f"unknown gitops target {target!r}")
            resolved_objects = resolved.get("objects")
            if not isinstance(resolved_objects, list):
                raise ValueError(f"gitops target {target!r} has no objects list")
            objects = [
                {k: v for k, v in obj.items() if k != "ekn"}
                for obj in resolved_objects
                if isinstance(obj, dict)
            ]
        else:
            generated = await proxy.attr("kubernetes").attr("generated").force_json()
            if not isinstance(generated, list):
                raise ValueError("kubernetes.generated did not evaluate to a list")
            objects = generated

        discriminator = await proxy.attr("kluctl").attr("discriminator").force_json()
        resource_priority = await proxy.attr("kluctl").attr("resourcePriority").force_json()
        sops_age_identities = await proxy.attr("kubernetes").attr("sopsAgeIdentities").force_json()

        return {
            "objects": objects,
            "discriminator": discriminator,
            "resource_priority": resource_priority,
            "sops_age_identities": sops_age_identities,
        }


async def realise_attr(
    file: str | PathLike[str] | None,
    flake_uri: str | None,
    attr_path: str,
) -> str:
    """Build the Nix value at `attr_path` and return its realised store path.

    Backs `ekn pushcache`: builds an arbitrary attribute (e.g. hetzkube's
    `kubenix.config.kluctl.projectDir`, whose rendered JSON keeps Nix string
    context on every store path it references) and realises that context --
    i.e. actually builds the full closure. This is the same thing
    kluctl.nix's preDeployScript already does with a bare `nix copy` for
    kluctl-applied objects, generalized so targets with no pre-apply hook of
    their own (e.g. an ArgoCD-synced GitOps target) can push their closure
    to a binary cache too, before anything tries to pull it.
    """
    async with (
        _session() as session,
        session.store() as store,
        session.eval(store, eval_settings=_profiler_eval_settings()) as eval_,
    ):
        if flake_uri is not None:
            proxy = await eval_.eval_flake(flake_uri)
        elif file is not None:
            proxy = await (await eval_.file(str(file))).auto_call()
        else:
            raise ValueError("specify --file or --flake")

        for name in attr_path.split("."):
            if not name:
                raise ValueError(f"empty segment in attr path: {attr_path!r}")
            proxy = proxy.attr(name)

        return await proxy.realise_string()


async def _validation_config(proxy: Any) -> dict:
    if await proxy.has_attr("config"):
        proxy = proxy.attr("config")

    # Deliberately does not force kubernetes.generated/generatedByPath/
    # gitopsTargets: Validate.run() applies manifests via
    # internal.manifestJSONFile (a derivation built straight from
    # kubernetes.generated, see internal.nix) and never reads the fields
    # this function returns beyond what's assembled below -- forcing them
    # here would just be wasted eval work.
    v = proxy.attr("validation")
    kubeadm_config = await v.attr("kubeadmConfig").force_json()
    pod_subnet = await v.attr("podSubnet").force_json()
    service_subnet = await v.attr("serviceSubnet").force_json()
    debug = await v.attr("debug").force_json()
    k8s_version = await proxy.attr("kubernetes").attr("package").attr("version").force_json()

    # kluctl.resourcePriority/discriminator are plain data (no build), used
    # by Validate.run()'s kr8s-based apply_and_prune instead of shelling out
    # to `kluctl deploy` -- see apply.py.
    resource_priority = await proxy.attr("kluctl").attr("resourcePriority").force_json()
    discriminator = await proxy.attr("kluctl").attr("discriminator").force_json()

    etcd_out = (await v.attr("etcdPackage").build()).get("out")
    kubeconform_out = (await v.attr("kubeconformPackage").build()).get("out")
    k8s_out = (await proxy.attr("kubernetes").attr("package").build()).get("out")
    manifest_out = (await proxy.attr("internal").attr("manifestJSONFile").build()).get("out")

    return {
        "config": {
            "kubernetes": {
                "package": {"version": k8s_version, "outPath": k8s_out},
            },
            "validation": {
                "kubeadmConfig": kubeadm_config,
                "podSubnet": pod_subnet,
                "serviceSubnet": service_subnet,
                "debug": debug,
                "etcdPackage": {"outPath": etcd_out},
                "kubeconformPackage": {"outPath": kubeconform_out},
            },
            "kluctl": {
                "resourcePriority": resource_priority,
                "discriminator": discriminator,
            },
            "internal": {"manifestJSONFile": {"outPath": manifest_out}},
        }
    }


async def evaluate_validation_file(
    file: str | PathLike[str], attr_path: str | None
) -> dict:
    async with (
        _session() as session,
        session.store() as store,
        session.eval(store, eval_settings=_profiler_eval_settings()) as eval_,
    ):
        proxy = await (await eval_.file(str(file))).auto_call()
        if attr_path:
            for name in attr_path.split("."):
                if not name:
                    raise ValueError(f"empty segment in attr path: {attr_path!r}")
                proxy = proxy.attr(name)
        return await _validation_config(proxy)


async def evaluate_validation_config(flake_uri: str, customer: str) -> dict:
    async with (
        _session() as session,
        session.store() as store,
        session.eval(store, eval_settings=_profiler_eval_settings()) as eval_,
    ):
        outputs = await eval_.eval_flake(flake_uri)
        system = await (await eval_.string("builtins.currentSystem")).force_json()
        proxy = outputs.attr("eknConfig").attr(str(system)).attr(customer)
        return await _validation_config(proxy)


__all__ = [
    "NixError",
    "evaluate_file",
    "evaluate_file_multi",
    "evaluate_flake",
    "evaluate_flake_ekn",
    "evaluate_generated_manifests",
    "evaluate_gitops_manifests",
    "evaluate_kubeapply_config",
    "evaluate_validation_config",
    "evaluate_validation_file",
]
