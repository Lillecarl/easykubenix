from __future__ import annotations

import cProfile
import os
import pstats
import sys
import time
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from os import PathLike
from pathlib import Path as _SyncPath
from typing import TYPE_CHECKING, Annotated, Any
from urllib.parse import SplitResult, urlsplit

import anyio
import structlog
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

from ekn.apply import DEFAULT_FIELD_MANAGER, DEFAULT_UNIT_LABEL
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
# "warn", not "error". The worker filters by level before anything reaches the
# session bus, so at "error" a `builtins.warn` -- and with it every
# `config.warnings` entry and every `mkRenamedOptionModule` deprecation notice
# -- was discarded inside Nix and could not be recovered on this side at any
# price. `_print_evaluation_warning` is what prints them once they arrive.
# Everything above warn is still suppressed, so this adds no progress chatter.
_log = structlog.get_logger()
_VERBOSITY: ContextVar[LogLevelInput] = ContextVar("_verbosity", default="warn")
_PRINT_BUILD_LOGS: ContextVar[bool] = ContextVar("_print_build_logs", default=False)


_NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]


class _OutPathInfo(BaseModel):
    out_path: str = Field(alias="outPath")


class _GitOpsTargetRef(BaseModel):
    """`deployment.units.<name>` as it reaches Python -- see easykubenix's
    gitops.nix, which narrows the submodule to the fields that survive
    serialization. Only `path` is read on the render path; `fieldManager` is
    read on the apply path (see `_unpack_gitops_target`)."""

    path: _NonEmptyStr


class TofuUnit(BaseModel):
    """Validated `deployment.tofuUnits` entry -- one `class = "tf"` deployment
    unit, as `ekn.tofu` needs it.

    `config_file` and `tofu` are store paths. Nix produces them without
    building them, so `evaluate_tofu_units` realises both before anything here
    is used; a path that has not been realised does not exist on disk.

    Deliberately a separate model from `GitOpsTargetEntry` rather than a wider
    version of it. That one requires `objects`, and three consumers read
    `.objects` off it -- widening the Kubernetes schema so half its entries
    carry none would cost each of them a branch and buy nothing.
    """

    name: _NonEmptyStr
    config_file: _NonEmptyStr = Field(alias="configFile")
    tofu: _NonEmptyStr
    #: The transitive closure, deepest first, this unit excluded. Every entry
    #: is itself a `tf` unit; easykubenix asserts that a dependency does not
    #: cross classes.
    dependencies: list[_NonEmptyStr] = Field(default_factory=list)
    target: _GitOpsTargetRef


class GitOpsBranches(BaseModel):
    """Validated `deployment.*` -- the branch pair, and the `tf` units whose
    rendered configuration is committed alongside the manifests.

    `source_branch` null disables the dual-commit source-snapshot feature for
    this instance, see `cli.py`'s `_gitops_branches`.

    `tofu_units` is here rather than beside `kubernetes.deploymentUnits`
    because that is where Nix puts it, and Nix puts it there because a `tf`
    unit renders no Kubernetes object. Defaulted, so an instance that declares
    none validates unchanged.
    """

    deploy_branch: _NonEmptyStr = Field(alias="deployBranch")
    source_branch: _NonEmptyStr | None = Field(default=None, alias="sourceBranch")
    tofu_units: dict[str, TofuUnit] = Field(default_factory=dict, alias="tofuUnits")


class GitOpsTargetEntry(BaseModel):
    """Validated `kubernetes.deploymentUnits` entry -- one named GitOps target's
    routed objects/raw files plus its resolved `deployment.units.<name>` entry,
    see `ekn.gitops.resolved_targets`."""

    target: _GitOpsTargetRef
    objects: list[dict[str, Any]]
    raw_files: list[_NonEmptyStr] = Field(default_factory=list, alias="rawFiles")


class _GitOpsKubernetesConfig(BaseModel):
    gitops_targets: dict[str, GitOpsTargetEntry] = Field(alias="deploymentUnits")


class _GitOpsManifestsConfig(BaseModel):
    kubernetes: _GitOpsKubernetesConfig
    git_ops: GitOpsBranches = Field(alias="deployment")


class GitOpsManifestsResult(BaseModel):
    """Return shape of `evaluate_gitops_manifests`, consumed by cli.py's
    `_gitops_branches`/`_gitops_file_groups`. Validating this at the Nix
    boundary (rather than `_dig()`-ing raw JSON apart by hand downstream)
    means a misconfigured `deployment.deployBranch` fails here, once, with a
    precise field-path error message."""

    config: _GitOpsManifestsConfig


class CacheConfigResult(BaseModel):
    #: `ekn.cacheTo`, always as a list. Nix accepts a bare string for the
    #: ordinary one-destination case; normalising here means every consumer
    #: has one shape to handle rather than a branch it can forget.
    cache_to: list[str] = Field(default_factory=list)
    cache_package_out: str | None
    # Seconds to allow the push; None waits forever. `ekn.cacheTimeoutSec`
    # says why there is a bound at all.
    cache_timeout_sec: float | None = None
    # `ekn.cacheAcceptNewHostKeys`. See `ssh_opts_for_push`.
    cache_accept_new_host_keys: bool = True


class SopsAgeIdentity(BaseModel):
    """Validated `kubernetes.sopsAgeIdentities` entry -- see
    `ekn.sops.ensure_age_identities`."""

    namespace: _NonEmptyStr
    secret_name: _NonEmptyStr = Field(alias="secretName")
    key: _NonEmptyStr = "key.txt"
    sops_config_file: _NonEmptyStr | None = Field(default=None, alias="sopsConfigFile")
    sops_files: list[_NonEmptyStr] = Field(default_factory=list, alias="sopsFiles")


class ApplyGroup(BaseModel):
    """One deployment unit's complete object set, and how to apply it.

    A `--target X` apply is a list of these: every unit X depends on, deepest
    first, and X itself last. A whole-instance apply is a single group with no
    unit.

    Grouped rather than one flat list because `fieldManager` is per unit. A
    bootstrap unit hands its objects to the controller that takes them over,
    and applying a dependency's objects under *that* manager would put two
    managers on the same fields the moment anyone applies the dependency on
    its own.
    """

    unit: str | None
    field_manager: str
    objects: list[dict[str, Any]]


class EngineWorkload(BaseModel):
    """One `deployment.engine.pause` entry -- see `ekn.enginepause`."""

    namespace: _NonEmptyStr
    name: _NonEmptyStr
    kind: _NonEmptyStr = "Deployment"


class KubeApplyConfigResult(BaseModel):
    groups: list[ApplyGroup]
    environment: str
    resource_priority: dict[str, int]
    sops_age_identities: list[SopsAgeIdentity]
    #: `deployment.handAppliedUnits` -- the units a whole-instance prune must
    #: leave alone.
    #:
    #: `None` means the evaluated configuration does not offer the option, so
    #: the exclusion set is *unknown*. A whole-instance prune then refuses to
    #: run rather than proceeding with an empty exclusion set, which would
    #: delete every hand-applied unit's objects. An easykubenix older than
    #: the option is the way this happens; see `KubeApply.run`.
    hand_applied_units: list[str] | None = Field(default=None, alias="handAppliedUnits")
    #: `deployment.declaredUnits`, read only to warn. See `apply._prune`.
    declared_units: list[str] | None = Field(default=None, alias="declaredUnits")
    #: `kubernetes.apiMappings` -- kind to apiVersion, so a prune scans kinds
    #: this apply no longer generates. See `apply_and_prune`'s `prune_kinds`.
    api_mappings: dict[str, str] = Field(default_factory=dict, alias="apiMappings")
    #: `deployment.engine.pause` -- the engine workloads a direct apply scales
    #: to zero. Empty means the configuration names none, which is the
    #: ordinary shape for an instance with no GitOps engine; `--pause-engine`
    #: refuses rather than silently pausing nothing. See `enginepause`.
    engine_pause: list[EngineWorkload] = Field(default_factory=list, alias="enginePause")
    #: `ekn.assertCached` -- the substituters to ask about every store path
    #: this apply names. Empty is off, and right for an instance with no
    #: CSI-backed store. See `storecheck.assert_fetchable`.
    assert_cached: list[str] = Field(default_factory=list, alias="assertCached")
    #: `deployment.unitFieldManagers` -- unit name to its `fieldManager`.
    #: Read **only while the engine is paused**; see `apply.field_manager_for`.
    unit_field_managers: dict[str, str] = Field(default_factory=dict, alias="unitFieldManagers")

    @property
    def objects(self) -> list[dict[str, Any]]:
        """Every object this apply sends, across all groups, in apply order."""
        return [obj for group in self.groups for obj in group.objects]

    @property
    def field_manager(self) -> str:
        """The manager the *named* unit applies as -- the last group's.

        A dependency keeps its own; see `ApplyGroup`.
        """
        return self.groups[-1].field_manager


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
    environment: str


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


# Nix's own log levels: 0 is an error, 1 a warning, and everything above is
# progress chatter. `builtins.warn` -- and so `lib.warn`, `lib.showWarnings`
# and every `config.warnings` entry -- arrives here as an `error` action at
# level 1.
_NIX_LOG_LEVEL_WARN = 1


def _print_evaluation_warning(event: LogEvent | None) -> None:
    """Forward Nix's evaluation warnings to stderr.

    Without this they are dropped outright: an evaluation warning reaches
    the client only as a log event on the session bus, and nothing was
    subscribed to that bus unless `--print-build-logs` was passed. So a
    config whose modules raise `warnings` -- an option deprecated by
    `mkRenamedOptionModule`, say -- deployed silently under `ekn` while
    `nix build` on the very same config printed the warning. Assertions were
    never affected, since a `throw` propagates as an evaluation error.

    Deliberately not gated on verbosity. A warning is not progress
    reporting; it is the module system telling the user their config needs
    attention, and needing a flag to see it defeats the point.
    """
    if event is None or event.action != "error":
        return
    info = event.error_info
    if info is None or info.get("level") != _NIX_LOG_LEVEL_WARN:
        return
    message = event.message_without_ansi
    if not message:
        return
    # Match `nix`'s own two prefixes so output lines up with what the same
    # config prints under `nix build`: warnings raised by an expression say
    # "evaluation warning", warnings from Nix itself just "warning".
    prefix = "evaluation warning" if info.get("is_from_expr") else "warning"
    sys.stderr.write(f"{prefix}: {message}\n")


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


PROFILE_TOP_N = 30
"""How many rows the stderr summary prints. The file has everything."""


@contextmanager
def python_profile() -> Generator[None]:
    """Profile the Python side of a run, when `EKN_PROFILE` is set.

    **The blind spot this closes.** `EKN_EVAL_PROFILER` profiles the Nix
    evaluator and `EKN_TIMING` names each stage, but neither can see inside
    a stage. Measured on a full nixlab2 render: 11-12s wall, of which
    `to_python(kubernetes.generated)` is 8.8-9.6s and the evaluator accounts
    for only 5.4s. So roughly 4s per render is import-from-derivation
    realisation plus marshalling 919 objects across the wire, and until this
    existed nothing could attribute any of it.

    `EKN_PROFILE=1` enables it; `EKN_PROFILE_FILE` says where the `pstats`
    dump goes, defaulting to `ekn.profile`. A summary goes to stderr too,
    because a bare `pstats` file needs another tool to read and the point is
    to see the answer.

    **Two backends, and the choice is about waiting.** `EKN_PROFILE=1` is
    cProfile, which counts calls. `EKN_PROFILE=pyinstrument` samples the wall
    clock and keeps an `await` in the frame that issued it, which is the only
    one of the two that can say anything useful about a command that spends
    its time waiting on an API server. Use it for `kubeapply` and `deploy`;
    use cProfile when the question is what runs too often. See
    `_cprofile_profile` and `_pyinstrument_profile`.

    It wraps `asyncio.run(command.run())` in `cli.py`, so it covers whichever
    command is running rather than evaluation alone.

    **It profiles this process only.** nanopynix runs the evaluator in its
    own worker, so the time this attributes to a `to_python` call is the
    marshalling and the waiting, not the evaluation inside it.
    `EKN_EVAL_PROFILER` covers the other side.

    **The two do not add up to the wall clock, and a third gap sits between
    them.** The eval profiler samples at call boundaries, so every nanopynix
    primop is invisible to it -- YAML parsing included. Measured on one
    nixlab2 render that is about 2.8s, between a 4.8s evaluator and a 7.6s
    stage. Neither profiler attributes it.

    Measure on a warm store. Render once unprofiled first, or a first-time
    import-from-derivation build lands inside the number and reads as
    evaluation.
    """
    backend = os.environ.get("EKN_PROFILE")
    if not backend:
        yield
        return
    if backend == "pyinstrument":
        with _pyinstrument_profile():
            yield
        return
    with _cprofile_profile():
        yield


@contextmanager
def _cprofile_profile() -> Generator[None]:
    """Deterministic profile: every call counted, `pstats` written.

    Right for the question "what does this do too much of". Wrong for a
    command that mostly waits: it traces each call, which weighs on a hot
    Python loop, and it has nowhere to put an `await` except the event loop's
    own frame.
    """
    profiler = cProfile.Profile()
    profiler.enable()
    try:
        yield
    finally:
        profiler.disable()
        destination = os.environ.get("EKN_PROFILE_FILE", "ekn.profile")
        profiler.dump_stats(destination)
        stats = pstats.Stats(profiler, stream=sys.stderr)
        sys.stderr.write(f"\n[EKN_PROFILE] Python-side profile, {PROFILE_TOP_N} rows by cumulative time\n")
        # Cumulative, not total: the question this answers is "which stage
        # costs the wall clock", and an await that spends its time in a
        # child frame has no total time of its own at all.
        stats.sort_stats("cumulative").print_stats(PROFILE_TOP_N)
        sys.stderr.write(f"[EKN_PROFILE] full profile written to {destination}\n")


@contextmanager
def _pyinstrument_profile() -> Generator[None]:
    """Sampling profile that keeps `await` in the frame that awaited.

    **Why a second backend rather than a better use of the first.** cProfile
    has one frame for every wait: measured on a render, `epoll.poll` was
    8.108s of an 11.039s profile. That is honest and useless -- it says the
    command waited, not what for. `ekn kubeapply` is almost entirely
    concurrent waiting on an API server, so cProfile would put nearly all of
    a deploy in that one line.

    pyinstrument samples the wall clock and is async-aware, so the wait is
    attributed to the frame that issued it. The same 8s becomes whichever
    apply, prune or discovery call is holding it.

    Sampling also costs a few percent where tracing costs much more, which
    matters when the thing being measured is a deploy that cannot be repeated
    cheaply.

    `EKN_PROFILE_INTERVAL` sets the sampling interval in seconds, default
    0.001. Raise it for a long deploy: the output shrinks with it, and a
    stage that only shows up below a millisecond is not the one costing the
    run.

    **It profiles this process only, exactly like the other backend.** The
    evaluator runs in a nanopynix worker, so time inside a `to_python` call
    is the marshalling and the waiting rather than the evaluation. Neither
    backend closes that gap; see the note on `python_profile`.

    Imported here rather than at module scope so the dependency is optional
    and the release build carries no profiler.
    """
    try:
        from pyinstrument import Profiler
    except ImportError:  # pragma: no cover -- the dev environment has it
        sys.stderr.write(
            "[EKN_PROFILE] EKN_PROFILE=pyinstrument needs the `profile` extra; "
            "the dev shell has it, a release build does not.\n"
        )
        yield
        return

    interval = float(os.environ.get("EKN_PROFILE_INTERVAL", "0.001"))
    profiler = Profiler(interval=interval, async_mode="enabled")
    profiler.start()
    try:
        yield
    finally:
        profiler.stop()
        # `_SyncPath`, because `Path` in this module is `anyio.Path` and its
        # `write_text` is a coroutine. This runs in a sync context manager.
        destination = _SyncPath(os.environ.get("EKN_PROFILE_FILE", "ekn.profile.html"))
        sys.stderr.write(f"\n[EKN_PROFILE] wall-clock profile, {interval}s interval\n")
        sys.stderr.write(profiler.output_text(unicode=True, color=True))
        destination.write_text(profiler.output_html())
        sys.stderr.write(f"[EKN_PROFILE] full profile written to {destination}\n")


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
        # One subscription either way: `_print_log_event` already prints every
        # event including the warnings, so subscribing both would print each
        # warning twice.
        sub = session.subscribe(_print_log_event if _PRINT_BUILD_LOGS.get() else _print_evaluation_warning)
        try:
            yield session
        finally:
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
    """Resolve to `{"config": {"kubernetes": {"deploymentUnits": ...},
    "deployment": {"deployBranch": ..., "sourceBranch": ...}}}`.

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

        gitops_proxy = proxy.attr("deployment")
        with timed_stage("gitops: to_python(kubernetes.deploymentUnits, deployment.deployBranch/sourceBranch)"):
            gitops_targets = await proxy.attr("kubernetes").attr("deploymentUnits").to_python()
            deploy_branch = await gitops_proxy.attr("deployBranch").to_python()
            source_branch = await gitops_proxy.attr("sourceBranch").to_python()
            tofu_units = await gitops_proxy.attr("tofuUnits").to_python()
        if not isinstance(tofu_units, dict):
            raise TypeError("deployment.tofuUnits did not evaluate to an object")

        # `configFile` only, deliberately, and not the wrapped `tofu` beside
        # it. A commit writes the rendered configuration and never runs
        # anything, so realising the binary would build OpenTofu and every
        # pinned provider for nothing. `evaluate_tofu_units` realises both,
        # because that path does run them.
        if tofu_units:
            with timed_stage("gitops: realise(deployment.tofuUnits.*.configFile)"):
                for name in tofu_units:
                    await gitops_proxy.attr("tofuUnits").attr(name).attr("configFile").realise_string()

        return GitOpsManifestsResult.model_validate(
            {
                "config": {
                    "kubernetes": {
                        "deploymentUnits": gitops_targets,
                    },
                    "deployment": {
                        "deployBranch": deploy_branch,
                        "sourceBranch": source_branch,
                        "tofuUnits": tofu_units,
                    },
                },
            }
        )


async def evaluate_tofu_units(
    file: str | PathLike[str] | None,
    flake_uri: str | None,
    customer: str | None,
    attr_path: str | None,
    target: str | None = None,
) -> list[TofuUnit]:
    """`deployment.tofuUnits`, built and ready to run.

    With *target*, the chain one `ekn tofu` run covers: that unit's dependency
    closure deepest first, then the unit itself. Without it, every `tf` unit
    the instance declares, which is what `ekn commit` writes.

    The realisation is the reason this cannot be a plain `to_python`. Nix
    reports `configFile` and `tofu` as store paths without building either, so
    without this every caller would get two paths that are not on disk.
    """
    async with (
        _session() as session,
        session.store() as store,
        session.eval(store, eval_settings=_profiler_eval_settings()) as eval_,
    ):
        proxy = await _resolve_proxy(eval_, file, flake_uri, customer, attr_path)
        if await proxy.has_attr("config"):
            proxy = proxy.attr("config")

        units_proxy = proxy.attr("deployment").attr("tofuUnits")
        with timed_stage("tofu: to_python(deployment.tofuUnits)"):
            declared = await units_proxy.to_python()
        if not isinstance(declared, dict):
            raise TypeError("deployment.tofuUnits did not evaluate to an object")

        if target is None:
            wanted = list(declared)
        else:
            entry = declared.get(target)
            if entry is None:
                known = ", ".join(sorted(declared)) or "none"
                raise ValueError(f'no deployment unit named "{target}" with class "tf". Declared: {known}')
            if not isinstance(entry, dict):
                raise TypeError(f"deployment.tofuUnits.{target} did not evaluate to an object")
            dependencies = entry.get("dependencies") or []
            if not isinstance(dependencies, list):
                raise TypeError(f"deployment.tofuUnits.{target}.dependencies is not a list")
            wanted = [*(str(name) for name in dependencies), target]

        # One realise per path, and the build happens here rather than at the
        # first subprocess. A failure to build is a Nix error with a Nix
        # message; a missing store path at `tofu init` time is a confusing
        # one.
        with timed_stage("tofu: realise(configFile, tofu)"):
            for name in wanted:
                unit_proxy = units_proxy.attr(name)
                await unit_proxy.attr("configFile").realise_string()
                await unit_proxy.attr("tofu").realise_string()

        return [TofuUnit.model_validate(declared[name]) for name in wanted]


def _unpack_gitops_target(
    gitops_targets: JsonValue,
    target: str,
) -> tuple[list[dict[str, Any]], list[tuple[JsonValue, str | None]], str, list[str]]:
    """Pull one `kubernetes.deploymentUnits` entry apart into the four things
    a `--target` apply needs: its objects (with `.ekn` routing metadata
    stripped), its raw-file paths, the field manager to apply as, and the
    units it depends on.

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
        raise TypeError("kubernetes.deploymentUnits did not evaluate to an object")
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
    field_manager = resolved_target.get("fieldManager")
    if not isinstance(field_manager, str):
        raise TypeError(f"gitops target {target!r} has no fieldManager")

    dependencies = resolved.get("dependencies") or []
    if not isinstance(dependencies, list):
        raise TypeError(f"gitops target {target!r} dependencies must be a list")

    # Every raw file in a unit's entry belongs to that unit, by construction:
    # `kubernetes.deploymentUnits` groups them by their own `deploymentUnit`.
    return (
        objects,
        [(path, target) for path in raw_file_paths],
        field_manager,
        [name for name in dependencies if isinstance(name, str)],
    )


def _raw_manifest_in_unit(path: str, unit: str | None) -> dict[str, Any]:
    """Load one `kubernetes.rawFiles` entry, carrying its unit label.

    Every other object gets `ekn.dev/deployment-unit` from Nix, where
    `stampRouted` writes it into the rendered manifest. A raw file is never
    parsed by Nix -- that is the whole point of one -- so there is nothing
    there to stamp, and the label has to be added here instead.

    It is not optional. `ekn` applies a routed raw file with the environment
    label like everything else, and the prune scope is decided by whether the
    unit label is *there*: without it, the next whole-instance `--prune` sees
    an object of this environment that belongs to no unit, does not find it
    in its own desired set, and deletes it. A bootstrap unit's raw file is
    typically ArgoCD's own `install.yaml`.

    Stamped on both apply paths, never on the commit path. Both applies write
    as the same field manager, so stamping on only one of them would make the
    label flip with whichever ran last -- the same trap `stampRouted` exists
    for. `ekn commit` leaves the file byte-identical, which is what
    `kubernetes.rawFiles` is for.
    """
    manifest = load_raw_manifest(path)
    if unit is None:
        return manifest
    metadata = manifest.get("metadata")
    metadata = dict(metadata) if isinstance(metadata, dict) else {}
    labels = metadata.get("labels")
    labels = dict(labels) if isinstance(labels, dict) else {}
    labels.setdefault(DEFAULT_UNIT_LABEL, unit)
    metadata["labels"] = labels
    return {**manifest, "metadata": metadata}


def _load_raw_manifests(paths: list[tuple[str, str | None]]) -> list[dict[str, Any]]:
    """Read `kubernetes.rawFiles` entries here, in Python.

    Not by having Nix `builtins.readFile` + `fromJSON`/eval them, which is
    exactly the round-trip `kubernetes.rawFiles` exists to avoid (see its
    description in easykubenix's kubernetes.nix). Once parsed, a
    raw-file-sourced manifest applies through
    `apply_and_prune`/`maybe_decrypt` identically to any other object.
    """
    return [_raw_manifest_in_unit(path, unit) for path, unit in paths if isinstance(path, str)]


def _unit_group(units: JsonValue, name: str) -> dict[str, Any]:
    """One deployment unit as an `ApplyGroup`'s fields."""
    objects, raw_file_paths, field_manager, _ = _unpack_gitops_target(units, name)
    return {
        "unit": name,
        "field_manager": field_manager,
        "objects": [*objects, *_load_raw_manifests([(str(p), u) for p, u in raw_file_paths])],
    }


async def evaluate_kubeapply_config(
    file: str | PathLike[str] | None,
    flake_uri: str | None,
    customer: str | None,
    attr_path: str | None,
    target: str | None,
) -> KubeApplyConfigResult:
    """Resolve the object list `ekn kubeapply` should apply, plus the
    `ekn.environment`/`ekn.resourcePriority` `apply_and_prune` needs
    and `kubernetes.sopsAgeIdentities` (SOPS age decrypt identities some
    consumer needs bootstrapped as a Secret -- see `ekn.sops.ensure_age_identities`).

    `target` narrows to one `kubernetes.deploymentUnits` entry's objects,
    `.ekn` routing metadata stripped, preceded by one group per unit that
    entry depends on; omitted, to_python's the full `kubernetes.generated`
    instead as a single group -- never both, so this only ever forces the one
    field it actually needs.

    `ekn.environment` does not follow that split: both scopes are the same
    environment. What separates them is the `ekn.dev/deployment-unit` label
    easykubenix renders onto a unit's objects, which the caller turns into a
    prune selector -- see `apply.prune_selector`.
    """
    async with (
        _session() as session,
        session.store() as store,
        session.eval(store, eval_settings=_profiler_eval_settings()) as eval_,
    ):
        proxy = await _resolve_proxy(eval_, file, flake_uri, customer, attr_path)
        if await proxy.has_attr("config"):
            proxy = proxy.attr("config")

        environment = await proxy.attr("ekn").attr("environment").to_python()

        if target:
            units = await proxy.attr("kubernetes").attr("deploymentUnits").to_python()
            _, _, _, dependencies = _unpack_gitops_target(units, target)
            # The dependency closure first, deepest first, then the named
            # unit. Each keeps its own `fieldManager`; see `ApplyGroup`.
            groups = [_unit_group(units, name) for name in [*dependencies, target]]
        else:
            generated = await proxy.attr("kubernetes").attr("generated").to_python()
            if not isinstance(generated, list):
                raise ValueError("kubernetes.generated did not evaluate to a list")
            raw_files = await proxy.attr("kubernetes").attr("rawFiles").to_python()
            if not isinstance(raw_files, list):
                raise ValueError("kubernetes.rawFiles did not evaluate to a list")
            raw_file_paths: list[tuple[str, str | None]] = []
            for entry in raw_files:
                if not isinstance(entry, dict):
                    continue
                entry_path = entry.get("path")
                if not isinstance(entry_path, str):
                    continue
                entry_unit = entry.get("deploymentUnit")
                raw_file_paths.append((entry_path, entry_unit if isinstance(entry_unit, str) else None))
            groups = [
                {
                    "unit": None,
                    # Only a deployment unit can name a field manager. A
                    # whole-`generated` apply has no successor to hand
                    # ownership to -- it *is* the steady state, and it runs
                    # again, so keeping conflict detection is right.
                    "field_manager": DEFAULT_FIELD_MANAGER,
                    "objects": [*generated, *_load_raw_manifests(raw_file_paths)],
                }
            ]

        deployment = proxy.attr("deployment")
        # Absent on an easykubenix older than the option. Left as `None`, which
        # a whole-instance `--prune` reads as "the exclusion set is unknown"
        # and refuses to run on. Defaulting to `[]` here would instead delete
        # every hand-applied unit's objects, silently and on the first run.
        hand_applied = (
            await deployment.attr("handAppliedUnits").to_python()
            if await deployment.has_attr("handAppliedUnits")
            else None
        )
        declared = (
            await deployment.attr("declaredUnits").to_python() if await deployment.has_attr("declaredUnits") else None
        )
        unit_managers = (
            await deployment.attr("unitFieldManagers").to_python()
            if await deployment.has_attr("unitFieldManagers")
            else {}
        )
        # `[]` and "the option does not exist" mean the same thing here, unlike
        # for `handAppliedUnits`: both say this configuration names no engine,
        # and `--pause-engine` refuses on either rather than pausing nothing.
        engine_pause = (
            await deployment.attr("engine").attr("pause").to_python() if await deployment.has_attr("engine") else []
        )
        ekn_opts = proxy.attr("ekn")
        assert_cached = (
            await ekn_opts.attr("assertCached").to_python() if await ekn_opts.has_attr("assertCached") else []
        )

        return KubeApplyConfigResult.model_validate(
            {
                "groups": groups,
                "environment": environment,
                "resource_priority": await proxy.attr("ekn").attr("resourcePriority").to_python(),
                "sops_age_identities": await proxy.attr("kubernetes").attr("sopsAgeIdentities").to_python(),
                "handAppliedUnits": hand_applied,
                "declaredUnits": declared,
                "apiMappings": await proxy.attr("kubernetes").attr("apiMappings").to_python(),
                "enginePause": engine_pause,
                "assertCached": assert_cached,
                "unitFieldManagers": unit_managers,
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

        declared = await proxy.attr("ekn").attr("cacheTo").to_python()
        if declared is None:
            return CacheConfigResult.model_validate({"cache_to": [], "cache_package_out": None})
        # A bare string is the ordinary one-destination case; normalise it
        # here so nothing downstream has to branch on the shape.
        cache_to = [declared] if isinstance(declared, str) else declared

        timeout_sec = await proxy.attr("ekn").attr("cacheTimeoutSec").to_python()
        accept_new = await proxy.attr("ekn").attr("cacheAcceptNewHostKeys").to_python()

        with timed_stage("cache-push: build ekn.cachePackage"):
            cache_package_out = (await proxy.attr("ekn").attr("cachePackage").build()).get("out")
        return CacheConfigResult.model_validate(
            {
                "cache_to": cache_to,
                "cache_package_out": cache_package_out,
                "cache_timeout_sec": timeout_sec,
                "cache_accept_new_host_keys": accept_new,
            }
        )


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


_MIB = 1024 * 1024

_SSH_STORE_SCHEMES = frozenset({"ssh", "ssh-ng"})


def ssh_destination(uri: str) -> SplitResult | None:
    """*uri* parsed, when it names a store Nix reaches over ssh. Else `None`.

    `urlsplit` and not a regex, so a `?ssh-key=...` query stays out of the
    host name and a `:2222` port stays out of it too.
    """
    split = urlsplit(uri)
    return split if split.scheme in _SSH_STORE_SCHEMES and split.hostname else None


def ssh_opts_for_push(uri: str, current: str | None, *, accept_new_host_keys: bool) -> str | None:
    """The `NIX_SSHOPTS` a push to *uri* wants, or `None` to keep *current*.

    Nix hands this variable to OpenSSH verbatim, so `accept-new` here makes
    the same trust-on-first-use decision an operator makes by hand after a
    push failed on an unknown key. `ekn deploy` cannot prompt, so the
    alternative is not a stricter deploy but a stopped one.
    `ekn.cacheAcceptNewHostKeys` holds the whole argument. Issue #18.

    A `current` that already names `StrictHostKeyChecking` is an explicit
    answer to this question, so it wins.
    """
    if not accept_new_host_keys or ssh_destination(uri) is None:
        return None
    if "StrictHostKeyChecking" in (current or ""):
        return None
    return f"{current or ''} -o StrictHostKeyChecking=accept-new".strip()


_SSH_PROBE_SECONDS = 5

#: Lower-cased fragments of OpenSSH's own stderr. Matching the text is what
#: there is: ssh exits 255 for every one of these.
_CHANGED_KEY_SIGN = "remote host identification has changed"
_UNKNOWN_KEY_SIGNS = ("host key verification failed", "host key is known")
_UNREACHABLE_SIGNS = (
    "connection refused",
    "connection timed out",
    "could not resolve hostname",
    "name or service not known",
    "no route to host",
    "network is unreachable",
    "operation timed out",
)


def _classify_ssh_stderr(stderr: str, target: str, port: int | None) -> str | None:
    at_port = f" -p {port}" if port else ""
    lowered = stderr.lower()
    if _CHANGED_KEY_SIGN in lowered:
        host = target.partition("@")[2] or target
        return (
            f"{target}'s host key changed since this machine recorded it. "
            f"Check why, then `ssh-keygen -R {host}` to drop the old one."
        )
    if any(sign in lowered for sign in _UNKNOWN_KEY_SIGNS):
        return (
            f"{target}'s host key is not trusted here, and that is enough to fail the push. "
            f"`ssh -o StrictHostKeyChecking=accept-new{at_port} {target}` accepts it, "
            f"and `ekn.cacheAcceptNewHostKeys` does the same on every push."
        )
    if any(sign in lowered for sign in _UNREACHABLE_SIGNS):
        return f"{target} did not answer ssh, so the host key is not the problem: {stderr.strip().splitlines()[-1]}"
    return None


async def ssh_failure_hint(uri: str) -> str | None:
    """Why a push to *uri* failed, said in the terms Nix does not use.

    Nix reports `failed to start SSH connection to '<host>'` for an unknown
    host key, a refused connection and a rejected login alike, and those need
    different answers. One ssh of our own, after the failure, separates them:
    it reads the same `ssh_config` and the same `known_hosts` files as the
    ssh Nix started, including `/etc/ssh/ssh_known_hosts`, which
    `ssh-keygen -F` never looks at. Issue #18.

    `StrictHostKeyChecking=yes`, never `accept-new`: a diagnosis must not
    change what the machine trusts, and `ekn.cacheAcceptNewHostKeys` has
    already had its turn by the time this runs.

    `None` where there is nothing to add -- a non-ssh destination, no ssh on
    `PATH`, or a host that answered and refused the login.
    """
    split = ssh_destination(uri)
    if split is None:
        return None
    target = f"{split.username}@{split.hostname}" if split.username else str(split.hostname)
    argv = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"ConnectTimeout={_SSH_PROBE_SECONDS}",
        *(["-p", str(split.port)] if split.port else []),
        target,
        "exit",
    ]
    try:
        with anyio.fail_after(_SSH_PROBE_SECONDS * 3):
            result = await anyio.run_process(argv, check=False)
    except (OSError, TimeoutError):
        return None
    return _classify_ssh_stderr(result.stderr.decode(errors="replace"), target, split.port)


@contextmanager
def _pushing_ssh_opts(uri: str, *, accept_new_host_keys: bool) -> Generator[None]:
    """Hold `ssh_opts_for_push`'s answer in the environment, then restore it.

    **Outside the session, not inside it.** The RPC worker that opens the
    destination store inherits this process's environment when it spawns, and
    libnixstore reads `NIX_SSHOPTS` when it starts ssh.
    """
    opts = ssh_opts_for_push(uri, os.environ.get("NIX_SSHOPTS"), accept_new_host_keys=accept_new_host_keys)
    if opts is None:
        yield
        return
    previous = os.environ.get("NIX_SSHOPTS")
    os.environ["NIX_SSHOPTS"] = opts
    try:
        yield
    finally:
        if previous is None:
            del os.environ["NIX_SSHOPTS"]
        else:
            os.environ["NIX_SSHOPTS"] = previous


async def _closure_size(source: Any, paths: list[str]) -> tuple[int, int]:
    """How many paths the copy covers, and their NAR size, read from *source*.

    **The whole closure, and not the part the destination lacks.** The count
    a reader wants is "3 of 19", and nanopynix's `Store` has no batch
    valid-path query: asking the destination would be one round trip for each
    path of the closure, over the same link the copy is about to use. A report
    that costs a round trip per path can be slower than the silence it
    replaces, so this reads the source alone. easykubenix issue #26 holds the
    missing half, and it needs `query_valid_paths` on the `Store` protocol.

    `compute_fs_closure` and `query_path_info` both read the local store, so
    the whole function is SQLite reads and no network.
    """
    closure: set[str] = set()
    for path in paths:
        closure.update(str(member) for member in await source.compute_fs_closure(path))
    nar_bytes = 0
    for member in closure:
        nar_bytes += (await source.query_path_info(member)).nar_size
    return len(closure), nar_bytes


async def push_closure_to_store(  # noqa: PLR0913 -- tracked complexity/arg-count debt, see TODO.md
    paths: list[str],
    to: str,
    *,
    substitute_on_destination: bool = True,
    check_sigs: bool = False,
    timeout_sec: float | None = None,
    accept_new_host_keys: bool = False,
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

    `timeout_sec` bounds the whole copy and raises `TimeoutError` when it
    runs out. There is a bound because a store URI naming a host that drops
    packets rather than refusing them leaves ssh in `SYN-SENT` until the
    kernel gives up on its SYN retries -- minutes, silently, and a deploy
    that looks hung rather than failed. See easykubenix issue #20.

    The deadline is outside the `async with`, so expiry unwinds the session
    as well: the worker holding the stuck connection goes with it, rather
    than being left to finish a copy nobody is waiting for.

    `accept_new_host_keys` covers an ssh destination whose key this machine
    has never seen -- see `ssh_opts_for_push`.
    """
    with _pushing_ssh_opts(to, accept_new_host_keys=accept_new_host_keys), anyio.fail_after(timeout_sec):
        async with (
            _session() as session,
            session.store() as source,
            session.store(uri=to) as dest,
        ):
            count, nar_bytes = await _closure_size(source, paths)
            _log.info(
                "copying closure",
                paths=count,
                mib=round(nar_bytes / _MIB, 1),
                to=to,
            )
            started = time.monotonic()
            await source.copy_closure(
                paths,
                dest,
                substitute=substitute_on_destination,
                check_sigs=check_sigs,
            )
            _log.info(
                "copied closure",
                paths=count,
                mib=round(nar_bytes / _MIB, 1),
                seconds=round(time.monotonic() - started, 1),
                to=to,
            )


async def _validation_config(proxy: Any) -> ValidationResult:
    if await proxy.has_attr("config"):
        proxy = proxy.attr("config")

    # Deliberately does not force kubernetes.generated/generatedByPath/
    # deploymentUnits: Validate.run() applies manifests via
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

        # ekn.resourcePriority/environment are plain data (no build), used by
        # Validate.run()'s kr8s-based apply_and_prune -- see apply.py. They used
        # to live under `kluctl.*` and be handed to `kluctl deploy`; nothing
        # about them was ever kluctl-specific.
        resource_priority = await proxy.attr("ekn").attr("resourcePriority").to_python()
        environment = await proxy.attr("ekn").attr("environment").to_python()

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
                    "environment": environment,
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
