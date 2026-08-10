from __future__ import annotations

import os
import sys
import time
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from os import PathLike
from typing import TYPE_CHECKING, Annotated, Any

from anyio import Path
from nanopynix import NixError, NixEvalSettings, NixSettings
from nanopynix.primops import yaml_primops
from nanopynix.rpc import Session
from nanopynix_helpers.eval_target import select_attr
from nanopynix_helpers.fod import (
    derivation_name_from_path,
    extract_fod_hash_mismatch,
    extract_unique_fod_hash_mismatch,
    find_fod_hash_literal,
    replace_fod_hash,
)
from pydantic import BaseModel, Field, StringConstraints

from ekn.gitops import load_raw_manifest

# `JsonValue` and `LogEvent` are type-only despite the pydantic models below:
# both are used in plain function signatures, never in a model field, so
# nothing resolves them at runtime. `runtime-evaluated-base-classes` in
# ruff-strict.toml is what keeps that distinction enforced if a field ever
# does use one.
if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Generator

    from nanopynix import AsyncEvalSession, AsyncValue
    from nanopynix.models import JsonValue, LogEvent
    from nanopynix.verbosity import LogLevelInput

_SESSION_SETTINGS = NixSettings()

# Every `_session()` call anywhere below reads these -- letting `ekn deploy`
# turn on verbosity/print-build-logs for its whole Validate -> cache-push ->
# Commit chain (each step opens its own Session) without threading extra
# parameters through every `evaluate_*` helper's signature.
_VERBOSITY: ContextVar[LogLevelInput] = ContextVar("_verbosity", default="error")
_PRINT_BUILD_LOGS: ContextVar[bool] = ContextVar("_print_build_logs", default=False)


_NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]


class _OutPathInfo(BaseModel):
    out_path: str = Field(alias="outPath")


class GitOpsBranches(BaseModel):
    """Validated `gitOps.deployBranch`/`gitOps.sourceBranch` -- `source_branch`
    null disables the dual-commit source-snapshot feature for this instance,
    see `cli.py`'s `_gitops_branches`."""

    deploy_branch: _NonEmptyStr = Field(alias="deployBranch")
    source_branch: _NonEmptyStr | None = Field(default=None, alias="sourceBranch")


class _GitOpsTargetRef(BaseModel):
    """`gitOps.targets.<name>` itself -- see easykubenix's gitops.nix. Just a
    `{path}` today, but modeled as its own submodule (not flattened to a bare
    string) since that's the real Nix shape and easykubenix may grow more
    fields on it later."""

    path: _NonEmptyStr


class GitOpsTargetEntry(BaseModel):
    """Validated `kubernetes.gitOpsTargets` entry -- one named GitOps target's
    routed objects/raw files plus its resolved `gitOps.targets.<name>` entry,
    see `ekn.gitops.resolved_targets`."""

    target: _GitOpsTargetRef
    objects: list[dict[str, Any]]
    raw_files: list[_NonEmptyStr] = Field(default_factory=list, alias="rawFiles")


class _GitOpsKubernetesConfig(BaseModel):
    gitops_targets: dict[str, GitOpsTargetEntry] = Field(alias="gitOpsTargets")


class _GitOpsManifestsConfig(BaseModel):
    kubernetes: _GitOpsKubernetesConfig
    git_ops: GitOpsBranches = Field(alias="gitOps")


class GitOpsManifestsResult(BaseModel):
    """Return shape of `evaluate_gitops_manifests`, consumed by cli.py's
    `_gitops_branches`/`_gitops_file_groups`. Validating this at the Nix
    boundary (rather than `_dig()`-ing raw JSON apart by hand downstream)
    means a misconfigured `gitOps.deployBranch` fails here, once, with a
    precise field-path error message."""

    config: _GitOpsManifestsConfig


class CacheConfigResult(BaseModel):
    cache_to: str | None
    cache_package_out: str | None


class SopsAgeIdentity(BaseModel):
    """Validated `kubernetes.sopsAgeIdentities` entry -- see
    `ekn.sops.ensure_age_identities`."""

    namespace: _NonEmptyStr
    secret_name: _NonEmptyStr = Field(alias="secretName")
    key: _NonEmptyStr = "key.txt"
    sops_config_file: _NonEmptyStr | None = Field(default=None, alias="sopsConfigFile")
    sops_files: list[_NonEmptyStr] = Field(default_factory=list, alias="sopsFiles")


class KubeApplyConfigResult(BaseModel):
    objects: list[dict[str, Any]]
    discriminator: str
    resource_priority: dict[str, int]
    sops_age_identities: list[SopsAgeIdentity]


class _ValidationPackageInfo(BaseModel):
    out_path: str = Field(alias="outPath")
    version: str


class _ValidationKubernetesConfig(BaseModel):
    package: _ValidationPackageInfo


class _ValidationInfo(BaseModel):
    kubeadm_config: dict[str, Any] = Field(alias="kubeadmConfig")
    pod_subnet: str = Field(alias="podSubnet")
    service_subnet: str = Field(alias="serviceSubnet")
    debug: bool
    etcd_package: _OutPathInfo = Field(alias="etcdPackage")
    kubeconform_package: _OutPathInfo = Field(alias="kubeconformPackage")


class _ApplyInfo(BaseModel):
    resource_priority: dict[str, int] = Field(alias="resourcePriority")
    discriminator: str


class _InternalInfo(BaseModel):
    manifest_json_file: _OutPathInfo = Field(alias="manifestJSONFile")


class _ValidationConfig(BaseModel):
    kubernetes: _ValidationKubernetesConfig
    validation: _ValidationInfo
    ekn: _ApplyInfo
    internal: _InternalInfo
    novalidate_keys: list[dict[str, str]] = Field(alias="novalidateKeys")


class ValidationResult(BaseModel):
    config: _ValidationConfig


class _FlakeEknKubernetesConfig(BaseModel):
    generated: list[dict[str, Any]]


class _FlakeEknConfig(BaseModel):
    kubernetes: _FlakeEknKubernetesConfig


class FlakeEknResult(BaseModel):
    config: _FlakeEknConfig


def _print_log_event(event: LogEvent | None) -> None:
    # `None` is the teardown marker. nanopynix's bus delivers the model on
    # both engines, so this no longer converts from the wire type.
    if event is None:
        return
    if event.result_type is not None and "BUILD_LOG" in event.result_type.name:
        line = event.args[-1] if event.args else None
        if isinstance(line, str):
            sys.stderr.write(line if line.endswith("\n") else line + "\n")
        return
    message = event.message_without_ansi
    if message:
        sys.stderr.write(message + "\n")


@contextmanager
def verbose_session(verbosity: LogLevelInput, *, print_build_logs: bool) -> Generator[None]:
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
async def _session() -> AsyncGenerator[Session]:
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


async def _resolve_proxy(
    eval_: AsyncEvalSession,
    file: str | PathLike[str] | None,
    flake_uri: str | None,
    customer: str | None,
    attr_path: str | None,
) -> AsyncValue:
    """Resolve --file/--flake[+--customer] into a proxy, then narrow by
    attr_path if given -- the branching prelude duplicated verbatim across
    evaluate_with_fod_update/evaluate_flake_ekn/evaluate_generated_manifests/
    evaluate_gitops_manifests/evaluate_kubeapply_config/evaluate_cache_config/
    evaluate_validation_config. Deliberately does not descend into `.config`
    -- callers that need that (all but evaluate_with_fod_update) do it
    themselves right after, since evaluate_with_fod_update's retry loop
    to_python's whatever attr_path picks directly and never descended into
    `.config`.
    """
    if flake_uri is not None:
        outputs = await eval_.eval_flake(flake_uri)
        if customer:
            system = await (await eval_.string("builtins.currentSystem")).to_python()
            proxy = outputs.attr("eknConfig").attr(str(system)).attr(customer)
        else:
            proxy = outputs
    elif file is not None:
        proxy = await (await eval_.file(str(file))).auto_call()
    else:
        raise ValueError("specify --file or --flake")

    if attr_path:
        proxy = await select_attr(proxy, attr_path)

    return proxy


async def evaluate_file(file: str | PathLike[str], attr_path: str | None) -> object:
    async with (
        _session() as session,
        session.store() as store,
        session.eval(store, eval_settings=_profiler_eval_settings()) as eval_,
    ):
        root = await (await eval_.file(str(file))).auto_call()

        proxy = root
        if attr_path:
            proxy = await select_attr(proxy, attr_path)

        return await proxy.to_python()


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
                proxy = await select_attr(proxy, attr_path)
            results.append(await proxy.to_python())
    return results


async def evaluate_with_fod_update(  # noqa: PLR0913 -- tracked complexity/arg-count debt, see TODO.md
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
                    proxy = await _resolve_proxy(eval_, file, flake_uri, customer, attr_path)
                    return await proxy.to_python()
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
            proxy = await select_attr(proxy, attr_path)

        return await proxy.to_python()


async def evaluate_flake_ekn(flake_uri: str, customer: str) -> FlakeEknResult:
    async with (
        _session() as session,
        session.store() as store,
        session.eval(store, eval_settings=_profiler_eval_settings()) as eval_,
    ):
        proxy = await _resolve_proxy(eval_, None, flake_uri, customer, None)
        if await proxy.has_attr("config"):
            proxy = proxy.attr("config")

        generated = await proxy.attr("kubernetes").attr("generated").to_python()
        return FlakeEknResult.model_validate(
            {
                "config": {
                    "kubernetes": {
                        "generated": generated,
                    },
                },
            }
        )


def _timing_enabled() -> bool:
    return bool(os.environ.get("EKN_TIMING"))


def _log_timing(label: str, elapsed: float) -> None:
    if _timing_enabled():
        sys.stderr.write(f"[EKN_TIMING] {label}: {elapsed:.3f}s\n")


@contextmanager
def timed_stage(label: str) -> Generator[None]:
    """Print `[EKN_TIMING] label: N.NNNs` to stderr on exit, when EKN_TIMING is
    set -- same env var/format `_log_timing` uses, as a context manager for
    call sites (cli.py's Deploy chain, `_validation_config`'s per-attr builds)
    that wrap a whole block rather than one already-measured span."""
    if not _timing_enabled():
        yield
        return
    start = time.monotonic()
    try:
        yield
    finally:
        _log_timing(label, time.monotonic() - start)


async def evaluate_generated_manifests(
    file: str | PathLike[str] | None,
    flake_uri: str | None,
    customer: str | None,
    attr_path: str | None,
) -> JsonValue:
    """Resolve a file or flake target down to `kubernetes.generated`.

    Unlike `evaluate_file`/`evaluate_flake`, this never to_python's the whole
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

        proxy = await _resolve_proxy(eval_, file, flake_uri, customer, attr_path)
        if await proxy.has_attr("config"):
            proxy = proxy.attr("config")

        t_before_force = time.monotonic()
        result = await proxy.attr("kubernetes").attr("generated").to_python()
        t_after_force = time.monotonic()
        _log_timing("to_python(kubernetes.generated)", t_after_force - t_before_force)
        _log_timing("total evaluate_generated_manifests", t_after_force - t_start)
        return result


async def evaluate_gitops_manifests(
    file: str | PathLike[str] | None,
    flake_uri: str | None,
    customer: str | None,
    attr_path: str | None,
) -> GitOpsManifestsResult:
    """Resolve to `{"config": {"kubernetes": {"gitOpsTargets": ...},
    "gitOps": {"deployBranch": ..., "sourceBranch": ...}}}`.

    Used by Diff/Commit/Deploy, which only ever read these fields via
    `_gitops_file_groups`/`_gitops_branches`. Diff/Commit previously went
    through the generic `_evaluate` -> `evaluate_file`/`evaluate_flake`,
    which to_python's the *entire* narrowed `config` (every option in
    every module, not just these fields) before `_dig()`-ing them out --
    forcing everything else was pure waste.
    """
    async with (
        _session() as session,
        session.store() as store,
        session.eval(store, eval_settings=_profiler_eval_settings()) as eval_,
    ):
        proxy = await _resolve_proxy(eval_, file, flake_uri, customer, attr_path)
        if await proxy.has_attr("config"):
            proxy = proxy.attr("config")

        gitops_proxy = proxy.attr("gitOps")
        with timed_stage("gitops: to_python(kubernetes.gitOpsTargets, gitOps.deployBranch/sourceBranch)"):
            gitops_targets = await proxy.attr("kubernetes").attr("gitOpsTargets").to_python()
            deploy_branch = await gitops_proxy.attr("deployBranch").to_python()
            source_branch = await gitops_proxy.attr("sourceBranch").to_python()
        return GitOpsManifestsResult.model_validate(
            {
                "config": {
                    "kubernetes": {
                        "gitOpsTargets": gitops_targets,
                    },
                    "gitOps": {
                        "deployBranch": deploy_branch,
                        "sourceBranch": source_branch,
                    },
                },
            }
        )


def _unpack_gitops_target(
    gitops_targets: JsonValue,
    target: str,
) -> tuple[list[dict[str, Any]], list[JsonValue], str]:
    """Pull one `kubernetes.gitOpsTargets` entry apart into the three things
    a `--target` apply needs: its objects (with `.ekn` routing metadata
    stripped), its raw-file paths, and its own discriminator.

    Split out of `evaluate_kubeapply_config` purely to keep that function
    under the complexity limit -- every branch here is a shape guard over a
    `to_python`'d value, which is inherently branchy and says nothing about
    the surrounding control flow.

    A wrong shape raises TypeError: it means the Nix side produced something
    this code cannot read, which is the same class of bug as passing the
    wrong type to a function. An unknown `target` is a plain ValueError --
    it is a typo on the command line, not a malformed evaluation, and is the
    one failure here a user is actually likely to hit.
    """
    if not isinstance(gitops_targets, dict):
        raise TypeError("kubernetes.gitOpsTargets did not evaluate to an object")
    if target not in gitops_targets:
        declared = ", ".join(sorted(gitops_targets)) or "(none)"
        raise ValueError(f"unknown gitops target {target!r}; declared targets: {declared}")
    resolved = gitops_targets[target]
    if not isinstance(resolved, dict):
        raise TypeError(f"gitops target {target!r} did not evaluate to an object")

    resolved_objects = resolved.get("objects")
    if not isinstance(resolved_objects, list):
        raise TypeError(f"gitops target {target!r} has no objects list")
    objects = [{k: v for k, v in obj.items() if k != "ekn"} for obj in resolved_objects if isinstance(obj, dict)]

    raw_file_paths = resolved.get("rawFiles") or []
    if not isinstance(raw_file_paths, list):
        raise TypeError(f"gitops target {target!r} rawFiles must be a list")

    resolved_target = resolved.get("target")
    if not isinstance(resolved_target, dict):
        raise TypeError(f"gitops target {target!r} has no resolved target config")
    discriminator = resolved_target.get("discriminator")
    if not isinstance(discriminator, str):
        raise TypeError(f"gitops target {target!r} has no discriminator")

    return objects, raw_file_paths, discriminator


async def evaluate_kubeapply_config(
    file: str | PathLike[str] | None,
    flake_uri: str | None,
    customer: str | None,
    attr_path: str | None,
    target: str | None,
) -> KubeApplyConfigResult:
    """Resolve the object list `ekn kubeapply` should apply, plus the
    discriminator/`ekn.resourcePriority` `apply_and_prune` needs
    and `kubernetes.sopsAgeIdentities` (SOPS age decrypt identities some
    consumer needs bootstrapped as a Secret -- see `ekn.sops.ensure_age_identities`).

    `target` narrows to one `kubernetes.gitOpsTargets` entry's objects,
    `.ekn` routing metadata stripped; omitted, to_python's the full
    `kubernetes.generated` instead -- never both, so this only ever forces
    the one field it actually needs.

    The discriminator follows the same split, because it is the prune scope
    and the prune scope has to match the apply scope: a `--target` apply uses
    that target's own `gitOps.targets.<name>.discriminator`, so pruning it
    cannot reach objects another target applied. Only the whole-`generated`
    apply uses the instance-wide `ekn.discriminator`.
    """
    async with (
        _session() as session,
        session.store() as store,
        session.eval(store, eval_settings=_profiler_eval_settings()) as eval_,
    ):
        proxy = await _resolve_proxy(eval_, file, flake_uri, customer, attr_path)
        if await proxy.has_attr("config"):
            proxy = proxy.attr("config")

        if target:
            objects, raw_file_paths, discriminator = _unpack_gitops_target(
                await proxy.attr("kubernetes").attr("gitOpsTargets").to_python(),
                target,
            )
        else:
            generated = await proxy.attr("kubernetes").attr("generated").to_python()
            if not isinstance(generated, list):
                raise ValueError("kubernetes.generated did not evaluate to a list")
            objects = generated
            raw_files = await proxy.attr("kubernetes").attr("rawFiles").to_python()
            if not isinstance(raw_files, list):
                raise ValueError("kubernetes.rawFiles did not evaluate to a list")
            raw_file_paths = [
                entry["path"] for entry in raw_files if isinstance(entry, dict) and isinstance(entry.get("path"), str)
            ]
            discriminator = await proxy.attr("ekn").attr("discriminator").to_python()

        # Read here, in Python -- not by having Nix `builtins.readFile` +
        # `fromJSON`/eval it, which is exactly the round-trip
        # `kubernetes.rawFiles` exists to avoid (see its description in
        # easykubenix's kubernetes.nix). Appended to `objects`: once
        # parsed, a raw-file-sourced manifest applies through
        # apply_and_prune/maybe_decrypt identically to any other object.
        objects = [*objects, *(load_raw_manifest(p) for p in raw_file_paths if isinstance(p, str))]

        resource_priority = await proxy.attr("ekn").attr("resourcePriority").to_python()
        sops_age_identities = await proxy.attr("kubernetes").attr("sopsAgeIdentities").to_python()

        return KubeApplyConfigResult.model_validate(
            {
                "objects": objects,
                "discriminator": discriminator,
                "resource_priority": resource_priority,
                "sops_age_identities": sops_age_identities,
            }
        )


async def evaluate_cache_config(
    file: str | PathLike[str] | None,
    flake_uri: str | None,
    customer: str | None,
    attr_path: str | None,
) -> CacheConfigResult:
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
        proxy = await _resolve_proxy(eval_, file, flake_uri, customer, attr_path)
        if await proxy.has_attr("config"):
            proxy = proxy.attr("config")

        cache_to = await proxy.attr("ekn").attr("cacheTo").to_python()
        if cache_to is None:
            return CacheConfigResult.model_validate({"cache_to": None, "cache_package_out": None})

        with timed_stage("cache-push: build ekn.cachePackage"):
            cache_package_out = (await proxy.attr("ekn").attr("cachePackage").build()).get("out")
        return CacheConfigResult.model_validate({"cache_to": cache_to, "cache_package_out": cache_package_out})


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

        proxy = await select_attr(proxy, attr_path)

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
            paths,
            dest,
            substitute=substitute_on_destination,
            check_sigs=check_sigs,
        )


async def _validation_config(proxy: Any) -> ValidationResult:
    if await proxy.has_attr("config"):
        proxy = proxy.attr("config")

    # Deliberately does not force kubernetes.generated/generatedByPath/
    # gitOpsTargets: Validate.run() applies manifests via
    # internal.manifestJSONFile (a derivation built straight from
    # kubernetes.generated, see internal.nix) and never reads the fields
    # this function returns beyond what's assembled below -- forcing them
    # here would just be wasted eval work.
    v = proxy.attr("validation")
    with timed_stage("validate: to_python cheap validation/apply fields"):
        kubeadm_config = await v.attr("kubeadmConfig").to_python()
        pod_subnet = await v.attr("podSubnet").to_python()
        service_subnet = await v.attr("serviceSubnet").to_python()
        debug = await v.attr("debug").to_python()
        k8s_version = await proxy.attr("kubernetes").attr("package").attr("version").to_python()

        # ekn.resourcePriority/discriminator are plain data (no build), used by
        # Validate.run()'s kr8s-based apply_and_prune -- see apply.py. They used
        # to live under `kluctl.*` and be handed to `kluctl deploy`; nothing
        # about them was ever kluctl-specific.
        resource_priority = await proxy.attr("ekn").attr("resourcePriority").to_python()
        discriminator = await proxy.attr("ekn").attr("discriminator").to_python()

        # Cheap -- just {kind, namespace, name} triples, not full objects (see
        # kubernetes.nix's novalidateKeys) -- lets Validate.run() skip applying
        # objects that can never be meaningfully verified in this ephemeral
        # harness without re-forcing the entire generated set a second time.
        novalidate_keys = await proxy.attr("kubernetes").attr("novalidateKeys").to_python()

    with timed_stage("validate: build etcdPackage"):
        etcd_out = (await v.attr("etcdPackage").build()).get("out")
    with timed_stage("validate: build kubeconformPackage"):
        kubeconform_out = (await v.attr("kubeconformPackage").build()).get("out")
    with timed_stage("validate: build kubernetes.package"):
        k8s_out = (await proxy.attr("kubernetes").attr("package").build()).get("out")
    with timed_stage("validate: build internal.manifestJSONFile (forces kubernetes.generated)"):
        manifest_out = (await proxy.attr("internal").attr("manifestJSONFile").build()).get("out")

    return ValidationResult.model_validate(
        {
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
                "ekn": {
                    "resourcePriority": resource_priority,
                    "discriminator": discriminator,
                },
                "internal": {"manifestJSONFile": {"outPath": manifest_out}},
                "novalidateKeys": novalidate_keys,
            },
        }
    )


async def evaluate_validation_file(
    file: str | PathLike[str],
    attr_path: str | None,
) -> ValidationResult:
    async with (
        _session() as session,
        session.store() as store,
        session.eval(store, eval_settings=_profiler_eval_settings()) as eval_,
    ):
        proxy = await (await eval_.file(str(file))).auto_call()
        if attr_path:
            proxy = await select_attr(proxy, attr_path)
        return await _validation_config(proxy)


async def evaluate_validation_config(flake_uri: str, customer: str) -> ValidationResult:
    async with (
        _session() as session,
        session.store() as store,
        session.eval(store, eval_settings=_profiler_eval_settings()) as eval_,
    ):
        proxy = await _resolve_proxy(eval_, None, flake_uri, customer, None)
        return await _validation_config(proxy)


__all__ = [
    "GitOpsManifestsResult",
    "GitOpsTargetEntry",
    "NixError",
    "SopsAgeIdentity",
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
    "timed_stage",
]
