from __future__ import annotations

import os
import sys
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from os import PathLike
from typing import Any

from anyio import Path
from nanopynix import NixError, NixEvalSettings, NixSettings, Session
from nanopynix.models import LogEvent
from nanopynix.primops import yaml_primops
from nanopynix.verbosity import LogLevelInput
from nanopynix_proto.nix.common import LogEvent as LogEventProto
from nanopynix_helpers.fod import (
    derivation_name_from_path,
    extract_fod_hash_mismatch,
    extract_unique_fod_hash_mismatch,
    find_fod_hash_literal,
    replace_fod_hash,
)

_SESSION_SETTINGS = NixSettings()

# Every `_session()` call anywhere below reads these -- letting `ekn deploy`
# turn on verbosity/print-build-logs for its whole Validate -> cache-push ->
# Commit chain (each step opens its own Session) without threading extra
# parameters through every `evaluate_*` helper's signature.
_VERBOSITY: ContextVar[LogLevelInput] = ContextVar("_verbosity", default="error")
_PRINT_BUILD_LOGS: ContextVar[bool] = ContextVar("_print_build_logs", default=False)


def _print_log_event(raw: object) -> None:
    if not isinstance(raw, LogEventProto):
        return
    event = LogEvent.from_proto(raw)
    if event.result_type is not None and "BUILD_LOG" in event.result_type.name:
        line = event.args[-1] if event.args else None
        if isinstance(line, str):
            sys.stderr.write(line if line.endswith("\n") else line + "\n")
        return
    message = event.message_without_ansi
    if message:
        sys.stderr.write(message + "\n")


@contextmanager
def verbose_session(verbosity: LogLevelInput, *, print_build_logs: bool) -> Iterator[None]:
    """Turn up nanopynix's own logging for every `_session()` opened inside
    this block -- real Nix build/eval progress that `nix run
    --print-build-logs` can't see (that flag only covers building the `ekn`
    CLI package itself, not what it does at runtime)."""
    verbosity_token = _VERBOSITY.set(verbosity)
    print_token = _PRINT_BUILD_LOGS.set(print_build_logs)
    try:
        yield
    finally:
        _VERBOSITY.reset(verbosity_token)
        _PRINT_BUILD_LOGS.reset(print_token)


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
        verbosity=_VERBOSITY.get(),
        # yaml_primops() (fromYAML/fromYAML11/*Stream/toYAML) are bundled
        # with nanopynix but opt-in, not auto-registered by Session -- needed
        # so Nix-side chart-rendering code (renderChart.nix) can parse
        # `helm template`'s IFD-built output in-process via fromYAML11Stream.
        primops=yaml_primops(),
    ) as session:
        sub = session.subscribe(_print_log_event) if _PRINT_BUILD_LOGS.get() else None
        try:
            yield session
        finally:
            if sub is not None:
                sub.unsubscribe()


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


async def evaluate_with_fod_update(
    file: str | PathLike[str] | None,
    flake_uri: str | None,
    customer: str | None,
    attr_path: str | None,
    *,
    source_file: str | PathLike[str],
    max_updates: int = 10,
) -> object:
    """Like `evaluate_file`/`evaluate_flake`, but auto-patch one fixed-output
    hash on mismatch and retry, up to `max_updates` times.

    Unlike `nanopynix_helpers.build.build_with_fod_update` (built around
    building one explicit derivation attr and verifying the mismatch belongs
    to that attr's closure), this forces an arbitrary JSON value -- e.g.
    kubenix's `kubernetes.crds`, which reads file content from several
    independent fetchers via IFD (`parseYAMLStream` etc.) rather than being a
    single derivation itself. There is no one target derivation to check
    closure membership against, so this trusts the caller to invoke it one
    mismatch at a time against a `source_file` whose fetcher is currently
    unpinned (e.g. `lib.fakeHash`) -- `extract_fod_hash_mismatch` still
    refuses to guess if Nix's diagnostic doesn't match its exact shape, and
    `find_fod_hash_literal` refuses if `source_file` has more than one
    plausible empty/placeholder hash literal.
    """
    source_path = Path(source_file)
    updates = 0
    async with (
        _session() as session,
        session.store() as store,
        session.eval(store, eval_settings=_profiler_eval_settings()) as eval_,
    ):
        while True:
            async with session.capture_logs() as logs:
                try:
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
                    return await proxy.force_json()
                except NixError as exc:
                    error = exc
            # The exception's own message is sometimes just a wrapper
            # ("Cannot build X, 1 dependency failed") when the mismatch
            # happened on a dependency FOD rather than the top-level target
            # -- the real two-line diagnostic instead arrives as a captured
            # log event, same as nanopynix_helpers.build.build_with_fod_update.
            mismatch = extract_fod_hash_mismatch(error.msg_without_ansi)
            if mismatch is None:
                mismatch = extract_unique_fod_hash_mismatch(
                    event.message_without_ansi for event in logs.events if event.message_without_ansi is not None
                )
            if mismatch is None:
                raise error
            if updates >= max_updates:
                raise RuntimeError(f"stopped after {max_updates} fixed-output hash updates") from error
            source = await source_path.read_text()
            literal = find_fod_hash_literal(
                source,
                mismatch.specified,
                derivation_name=derivation_name_from_path(mismatch.drv_path),
            )
            updated = replace_fod_hash(source, literal, mismatch.got)
            await source_path.write_text(updated)
            updates += 1
            await eval_.reset_file_cache()


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


async def evaluate_cache_config(
    file: str | PathLike[str] | None,
    flake_uri: str | None,
    customer: str | None,
    attr_path: str | None,
) -> dict:
    """Resolve `ekn.cacheTo` and build `ekn.cachePackage`, for `Deploy`'s
    automatic pre-git-push cache push (see `cli.py`'s `Deploy.run`).

    Never forces the whole config, matching `evaluate_gitops_manifests`'s
    rationale. `cachePackage`'s closure is realized by `.build()`-ing it here
    (its derivation inputs are every store path embedded anywhere in
    `kubernetes.generated`, via Nix string context) -- `copy_closure` then
    only needs this one output path; libnixstore computes the rest of the
    closure to copy from the store's own reference graph, not from Python.
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

        cache_to = await proxy.attr("ekn").attr("cacheTo").force_json()
        if cache_to is None:
            return {"cache_to": None, "cache_package_out": None}

        cache_package_out = (await proxy.attr("ekn").attr("cachePackage").build()).get("out")
        return {"cache_to": cache_to, "cache_package_out": cache_package_out}


async def realise_attr(
    file: str | PathLike[str] | None,
    flake_uri: str | None,
    attr_path: str,
) -> str:
    """Build the Nix value at `attr_path` and return its realised store path.

    Backs `ekn pushcache`: builds an arbitrary user-specified attribute
    (whose rendered value keeps Nix string context on every store path it
    references) and realises that context -- i.e. actually builds the full
    closure -- so `push_closure_to_store` has a real path to copy.
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


async def push_closure_to_store(
    paths: list[str],
    to: str,
    *,
    substitute_on_destination: bool = True,
    check_sigs: bool = False,
) -> None:
    """Copy the closure of already-realised `paths` to the store at `to`.

    Pure store-to-store copy, no evaluation involved -- backs both
    `ekn pushcache` (paths from `realise_attr`) and `Deploy`'s automatic
    pre-git-push cache push (paths from `evaluate_cache_config`). Opens a
    fresh source + destination store pair in one session (a copy_closure
    destination must share the session/worker of the store it's called
    against -- see nanopynix's Store.copy_closure), rather than reusing
    whatever session/store originally realised the paths -- the physical
    Nix store on disk is what actually matters, not which in-process Store
    handle built it.
    """
    async with (
        _session() as session,
        session.store() as source,
        session.store(uri=to) as dest,
    ):
        await source.copy_closure(
            paths, dest,
            substitute=substitute_on_destination,
            check_sigs=check_sigs,
        )


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

    # Cheap -- just {kind, namespace, name} triples, not full objects (see
    # kubernetes.nix's novalidateKeys) -- lets Validate.run() skip applying
    # objects that can never be meaningfully verified in this ephemeral
    # harness without re-forcing the entire generated set a second time.
    novalidate_keys = await proxy.attr("kubernetes").attr("novalidateKeys").force_json()

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
            "novalidateKeys": novalidate_keys,
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
    "evaluate_with_fod_update",
]
