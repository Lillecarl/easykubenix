from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path as _Path
from typing import TYPE_CHECKING, Literal, NoReturn, cast

import anyio
import anyio.to_thread
import kr8s.asyncio
import pygit2
import rich.traceback
import structlog
from anyio import Path
from nanopynix import NixError
from nanopynix.models import JsonValue
from nanopynix.primops import from_yaml11_stream, from_yaml_stream, to_yaml
from pydantic import TypeAdapter, ValidationError

from ekn._cli import Command, build_parser, complete, dispatch, opt, pos
from ekn.apply import apply_and_prune
from ekn.clusterdiff import cluster_diff
from ekn.eval import (
    GitOpsManifestsResult,
    evaluate_cache_config,
    evaluate_file,
    evaluate_flake,
    evaluate_flake_ekn,
    evaluate_generated_manifests,
    evaluate_gitops_manifests,
    evaluate_kubeapply_config,
    evaluate_validation_config,
    evaluate_validation_file,
    evaluate_with_fod_update,
    push_closure_to_store,
    realise_attr,
    timed_stage,
    verbose_session,
)
from ekn.git import (
    ConcurrentDeployError,
    PreparedCommits,
    commit_manifests,
    diff_manifests,
    finalize_branches,
    prepare_deploy_and_source_commits,
    rollback_branches,
    try_jj_status,
)
from ekn.gitops import (
    GitOpsTargetError,
    branches as gitops_branches,
    file_groups as gitops_file_groups,
    flatten_manifests,
)
from ekn.sops import ensure_age_identities, maybe_decrypt
from ekn.validation import EphemeralControlPlane, exec_capture, prepare_validation_objects

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

_log = structlog.get_logger()

#: What `ekn deploy --verbosity` accepts. nanopynix' own `LogLevelInput` is
#: `str | int | LogLevel`, which says nothing to a parser; these are the eight
#: names Nix knows, so argparse rejects anything else and the shell offers them
#: on Tab.
type LogLevel = Literal["error", "warn", "notice", "info", "talkative", "chatty", "debug", "vomit"]


def _report_nix_error(exc: NixError) -> NoReturn:
    """Log a Nix eval/build failure and exit(1) -- shared by every Command
    that evaluates Nix and can't do anything useful once it fails."""
    _log.error(exc.msg_without_ansi)
    raise SystemExit(1) from exc


def _report_validation_error(context: str, exc: ValidationError) -> NoReturn:
    """Log a pydantic ValidationError against `context` (e.g. "GitOps
    config") and exit(1) -- shared by every Command whose evaluate_* result
    is validated into a pydantic model."""
    _log.error(f"invalid {context}: {exc}")
    raise SystemExit(1) from exc


def _report_server_error(action: str, exc: kr8s.ServerError) -> NoReturn:
    """Log a kr8s.ServerError with its response body and exit(1).

    kr8s only extracts the JSON `message` field for 4xx errors (see
    kr8s._api.Api.call_api) -- for 5xx it falls back to str(httpx
    exception), which omits the API server's actual response body. Surface
    it ourselves since that body is usually the only clue for a 500.
    """
    body = exc.response.text if exc.response is not None else None
    _log.error("%s failed\n%s\nresponse body: %s", action, exc, body)
    raise SystemExit(1) from exc


def _parse_flake(flake_ref: str) -> tuple[str, str | None]:
    if "#" in flake_ref:
        uri, _, customer = flake_ref.partition("#")
    else:
        uri, customer = flake_ref, None
    if not uri or uri == ".":
        uri = str(_Path.cwd())
    return uri, customer


async def _evaluate(
    file: _Path | None,
    flake: str | None,
    attr: str | None,
) -> object:
    try:
        if flake is not None:
            uri, customer = _parse_flake(flake)
            if customer:
                return await evaluate_flake_ekn(uri, customer)
            return await evaluate_flake(uri, attr)
        if file is not None:
            return await evaluate_file(file, attr)
        raise ValueError("specify --file or --flake")
    except NixError as exc:
        _report_nix_error(exc)


async def _evaluate_gitops(
    file: _Path | None,
    flake: str | None,
    attr: str | None,
) -> GitOpsManifestsResult:
    """Like `_evaluate`, but only forces kubernetes.gitOpsTargets -- the
    only field Diff/Commit/Deploy read via `gitops.file_groups`."""
    try:
        uri, customer = _parse_flake(flake) if flake is not None else (None, None)
        return await evaluate_gitops_manifests(file, uri, customer, attr)
    except NixError as exc:
        _report_nix_error(exc)
    except ValidationError as exc:
        _report_validation_error("GitOps config", exc)


class NixCommand(Command):
    """A command that reads a Nix entry point: a file, or a flake reference.

    **Declared once, and inherited.** `Command.__init_subclass__` walks the MRO,
    so a subclass gets these without redeclaring them. Under clypi each command
    parsed only what its own class body declared, so the same lines stood in
    front of every command in this file, `inherited=True` and all.
    """

    file: _Path | None = opt(None, short="f", help="Nix file to evaluate.")
    flake: str | None = opt(
        None, help="Flake reference (e.g. '.#myconfig'). Evaluates outputs.eknConfig.<system>.<attr>."
    )


class AttrCommand(NixCommand):
    """A Nix command that also takes a path inside the evaluated result.

    Separate from `NixCommand` for one command: `pushcache` has an `--attr` of
    its own, which the caller must give and which names what to build. A
    subclass cannot narrow an inherited `str | None` to `str`.
    """

    attr: str | None = opt(None, short="A", help="Dot-separated attribute path within the evaluation result.")


class Eval(AttrCommand):
    """Evaluate Nix and dump JSON."""

    update_fod: bool = opt(
        False,
        help="On a fixed-output hash mismatch, patch --source-file's plain-string hash literal with Nix's reported hash and retry.",
    )
    source_file: _Path | None = opt(
        None,
        help="Nix file containing the fixed-output hash literal to patch (required with --update-fod).",
    )

    async def run(self) -> None:
        if self.update_fod:
            if self.source_file is None:
                _log.error("--update-fod requires --source-file")
                raise SystemExit(1)
            uri, customer = _parse_flake(self.flake) if self.flake is not None else (None, None)
            try:
                result = await evaluate_with_fod_update(
                    self.file,
                    uri,
                    customer,
                    self.attr,
                    source_file=self.source_file,
                )
            except NixError as exc:
                _report_nix_error(exc)
        else:
            result = await _evaluate(self.file, self.flake, self.attr)
        json.dump(result, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")


class Render(AttrCommand):
    """Render Kubernetes manifests as YAML on stdout."""

    async def run(self) -> None:
        uri, customer = _parse_flake(self.flake) if self.flake is not None else (None, None)
        try:
            manifests = await evaluate_generated_manifests(self.file, uri, customer, self.attr)
        except NixError as exc:
            _report_nix_error(exc)
        if not isinstance(manifests, list):
            _log.error("expected a list result, got %s", type(manifests).__name__)
            raise SystemExit(1)
        for _, content in flatten_manifests(manifests):
            sys.stdout.write("---\n")
            sys.stdout.write(content)


class Diff(AttrCommand):
    """Diff GitOps-routed manifests against the deploy branch."""

    async def run(self) -> None:
        deploy_branch, _source_branch, files = await _resolve_gitops(self.file, self.flake, self.attr)
        try:
            diff_output = diff_manifests(".", deploy_branch, files)
        except Exception as exc:
            _log.error("diff failed: %s", exc)
            raise SystemExit(1) from exc
        if diff_output is not None:
            sys.stdout.write(diff_output)
        else:
            _log.info("no differences")
        await try_jj_status(".")


async def _resolve_gitops(
    file: _Path | None,
    flake: str | None,
    attr: str | None,
) -> tuple[str, str | None, list[tuple[str, str]]]:
    """Evaluate GitOps manifests and return `(deploy_branch, source_branch,
    files)` -- one branch pair per easykubenix instance, see
    `gitops.branches`."""
    result = await _evaluate_gitops(file, flake, attr)
    try:
        files = gitops_file_groups(result)
    except (GitOpsTargetError, TypeError) as exc:
        _log.error("GitOps routing failed: %s", exc)
        raise SystemExit(1) from exc
    deploy_branch, source_branch = gitops_branches(result)
    return deploy_branch, source_branch, files


def _default_commit_message(attr: str | None) -> str:
    try:
        repo = pygit2.Repository(".")
        head_sha = str(repo.head.target)[:7]
    except Exception:
        head_sha = "unknown"
    return f"ekn: render manifests from {attr or 'root'} @ {head_sha}"


async def _finalize_commit(  # noqa: PLR0913 -- tracked complexity/arg-count debt, see TODO.md
    deploy_branch: str,
    source_branch: str | None,
    files: list[tuple[str, str]],
    message: str,
    prepared: PreparedCommits | None,
    *,
    push: bool,
    remote: str,
) -> None:
    """Write the rendered manifests to `deploy_branch` (and, if
    `source_branch` is set, the paired source snapshot too), then push if
    requested.

    If `prepared` is given (built earlier via
    `prepare_deploy_and_source_commits`, e.g. before `Deploy` runs Validate),
    only the finalize (ref-move) step happens here. Otherwise prepare and
    finalize happen back-to-back -- `Commit` invoked directly has no
    intervening verification step to prepare ahead of.
    """
    if source_branch is None:
        try:
            commit_id = commit_manifests(".", deploy_branch, files, message)
        except Exception as exc:
            _log.error("commit failed: %s", exc)
            raise SystemExit(1) from exc
        _log.info("committed to %s @ %s", deploy_branch, commit_id)
    else:
        try:
            if prepared is None:
                prepared = prepare_deploy_and_source_commits(
                    ".",
                    deploy_branch,
                    source_branch,
                    files,
                    message,
                )
            finalize_branches(".", deploy_branch, source_branch, prepared)
        except ConcurrentDeployError as exc:
            _log.error(str(exc))
            raise SystemExit(1) from exc
        except Exception as exc:
            _log.error("commit failed: %s", exc)
            raise SystemExit(1) from exc
        _log.info(
            "committed to %s @ %s (source %s @ %s)",
            deploy_branch,
            prepared.deploy_oid,
            source_branch,
            prepared.source_oid,
        )
    if push:
        await _git_push(remote, deploy_branch, source_branch)


class Commit(AttrCommand):
    """Render manifests and write them to the GitOps deploy (and paired
    source) branch."""

    message: str | None = opt(None, short="m", help="Commit message.")
    push: bool = opt(
        False,
        help="git push the committed GitOps branch(es) to their remote afterwards -- "
        "commits are only ever made locally, and ArgoCD/Flux read from the remote.",
    )
    remote: str = opt("origin", help="Remote to push GitOps branch(es) to (with --push).")

    async def run(self) -> None:
        deploy_branch, source_branch, files = await _resolve_gitops(self.file, self.flake, self.attr)
        message = self.message or _default_commit_message(self.attr)
        await _finalize_commit(
            deploy_branch,
            source_branch,
            files,
            message,
            None,
            push=self.push,
            remote=self.remote,
        )
        await try_jj_status(".")


async def _git_push(remote: str, deploy_branch: str, source_branch: str | None) -> None:
    branches = [deploy_branch] if source_branch is None else [deploy_branch, source_branch]
    _log.info(f"pushing {', '.join(branches)} to {remote}")
    # --atomic (a no-op with a single ref) keeps deploy_branch/source_branch
    # from ever updating independently on the remote when both are pushed.
    proc = await asyncio.create_subprocess_exec("git", "push", "--atomic", remote, *branches)
    rc = await proc.wait()
    if rc != 0:
        _log.error(f"git push --atomic {remote} {' '.join(branches)} failed (rc={rc})")
        raise SystemExit(1)


async def _push_cache(  # noqa: PLR0913 -- tracked complexity/arg-count debt, see TODO.md
    attr: str, file: _Path | None, flake: str | None, to: str, *, substitute_on_destination: bool, check_sigs: bool
) -> None:
    try:
        path = await realise_attr(file, flake, attr)
        await push_closure_to_store(
            [path],
            to,
            substitute_on_destination=substitute_on_destination,
            check_sigs=check_sigs,
        )
    except NixError as exc:
        _report_nix_error(exc)
    _log.info(f"pushed {path} to {to}")


async def _push_ekn_cache(file: _Path | None, flake: str | None, attr: str | None, *, allow_failure: bool) -> None:
    """`Deploy`'s automatic pre-git-push cache push, sourced entirely from
    Nix config (`ekn.cacheTo`/`ekn.cachePackage`) -- no CLI flags needed.

    Must run and (by default) succeed *before* the git commit/push that
    triggers GitOps sync: CSI-mounted store paths referenced in the
    manifests about to be applied need to already be substitutable, or
    those pods fail to start the moment ArgoCD/Flux syncs.
    """
    try:
        uri, customer = _parse_flake(flake) if flake is not None else (None, None)
        cfg = await evaluate_cache_config(file, uri, customer, attr)
    except NixError as exc:
        _report_nix_error(exc)
    except ValidationError as exc:
        _report_validation_error("cache config", exc)

    cache_to = cfg.cache_to
    if cache_to is None:
        _log.info("ekn.cacheTo is null -- skipping pre-deploy cache push")
        return
    cache_package_out = cfg.cache_package_out
    if cache_package_out is None:
        _log.error("ekn.cachePackage's build produced no 'out' output")
        raise SystemExit(1)

    try:
        await push_closure_to_store([cache_package_out], cache_to)
    except NixError as exc:
        if allow_failure:
            _log.warning(
                f"cache push to {cache_to} failed, continuing anyway (--cache-allow-failure)\n{exc.msg_without_ansi}"
            )
            return
        _report_nix_error(exc)
    _log.info(f"pushed {cache_package_out} to {cache_to}")


class Validate(AttrCommand):
    """Boot real etcd+kube-apiserver, apply manifests, and run kubeconform."""

    async def run(self) -> None:
        if self.file is not None:
            cfg = await evaluate_validation_file(self.file, self.attr)
        else:
            uri, customer = _parse_flake(str(self.flake))
            if customer is None:
                _log.error("--flake must include a customer attr (e.g. '.#myapp')")
                raise SystemExit(1)
            cfg = await evaluate_validation_config(uri, customer)
        c = cfg.config

        manifest_path = c.internal.manifest_json_file.out_path
        novalidate_keys = {(k["kind"], k["namespace"], k["name"]) for k in c.novalidate_keys}

        async with EphemeralControlPlane(
            k8s_bin=c.kubernetes.package.out_path + "/bin",
            etcd_bin=c.validation.etcd_package.out_path + "/bin",
            kubeconform_bin=c.validation.kubeconform_package.out_path + "/bin",
            service_subnet=c.validation.service_subnet,
            kubeadm_config=c.validation.kubeadm_config,
        ) as plane:
            _log.info("applying manifests")
            objects = await prepare_validation_objects(manifest_path, novalidate_keys)
            kr8s_api = await kr8s.asyncio.api(kubeconfig=plane.kubeconfig)
            try:
                await apply_and_prune(
                    objects,
                    api=kr8s_api,
                    discriminator=c.ekn.discriminator,
                    resource_priority=c.ekn.resource_priority,
                )
            except kr8s.ServerError as exc:
                _report_server_error("apply", exc)
            except Exception as exc:
                _log.error("apply failed\n%s", exc)
                raise SystemExit(1) from exc

            _log.info("dumping OpenAPI schema")
            rc, out, err = await exec_capture("kubectl", "get", "--raw", "/openapi/v2", env=plane.env)
            if rc != 0:
                _log.error("OpenAPI schema dump failed\n%s", err)
                raise SystemExit(1)
            await Path(plane.schema_file).write_text(out)

            _log.info("running kubeconform")
            manifest_data = await Path(manifest_path).read_text()
            rc, out, err = await exec_capture(
                "kubeconform",
                f"-schema-location={plane.schema_file}",
                "-summary",
                stdin=manifest_data,
                env=plane.env,
            )
            sys.stdout.write(out)
            if rc != 0:
                _log.error("%s\nkubeconform verification failed", err)
                raise SystemExit(1)

            _log.info("Your manifests are as valid as they can be against Kubernetes %s", c.kubernetes.package.version)


class Deploy(Commit):
    """Verify, push the pre-deploy cache, commit, and push -- the whole
    release in one command.

    Chains Validate (unless --no-verify) -> cache push (`ekn.cacheTo`/
    `ekn.cachePackage`, read straight from Nix config -- see
    `evaluate_cache_config`; a no-op if `ekn.cacheTo` is unset) -> Commit
    (render + write GitOps branches, git-pushed with --push).

    The cache push runs *before* the git commit/push deliberately: ArgoCD/
    Flux may sync the instant the branch updates, and CSI-mounted store
    paths referenced in the manifests need to already be substitutable at
    that point, not eventually -- a failed cache push aborts the deploy by
    default (see --cache-allow-failure).
    """

    # `--file`, `--flake`, `--attr`, `--message`, `--push` and `--remote` are
    # not here, and that is the point: `Command.__init_subclass__` walks the
    # MRO, so `deploy` parses every option `commit` declares. clypi parsed only
    # what the class body itself declared, so all six stood here as well.
    no_verify: bool = opt(
        False,
        help="Skip temporary API-server and kubeconform verification.",
    )
    cache_allow_failure: bool = opt(
        False,
        help="Log a warning and continue if the pre-deploy cache push fails, instead of aborting. "
        "Off by default -- CSI-mounted pods will fail to start if referenced store paths were "
        "never pushed, so a failed push should normally block the deploy.",
    )
    verbosity: LogLevel = opt(
        "error",
        short="v",
        help="Nix log verbosity for every eval/build nanopynix does during this deploy "
        "(error, warn, notice, info, talkative, chatty, debug, vomit). `nix run "
        "--print-build-logs` only covers building the ekn CLI package itself, not what "
        "it does at runtime -- this is the runtime equivalent.",
    )
    print_build_logs: bool = opt(
        False,
        help="Stream build/eval log lines from nanopynix's worker to stderr as they "
        "happen, for visibility into what's taking long during Validate/cache-push/Commit.",
    )

    async def run(self) -> None:
        with verbose_session(self.verbosity, print_build_logs=self.print_build_logs):
            deploy_branch, source_branch, files = await _resolve_gitops(self.file, self.flake, self.attr)
            message = self.message or _default_commit_message(self.attr)

            # Prepared *before* Validate/cache-push run (not just before the
            # git push) -- pygit2 only ever builds loose objects here, no
            # ref is moved until `_finalize_commit`'s `finalize_branches`
            # call below, so a failed Validate/cache-push leaves no trace.
            prepared: PreparedCommits | None = None
            if source_branch is not None:
                with timed_stage("deploy: prepare commits"):
                    prepared = prepare_deploy_and_source_commits(
                        ".",
                        deploy_branch,
                        source_branch,
                        files,
                        message,
                    )

            if not self.no_verify:
                with timed_stage("deploy: validate (total)"):
                    await Validate.run(cast("Validate", self))
            with timed_stage("deploy: cache-push (total, incl. network copy)"):
                await _push_ekn_cache(self.file, self.flake, self.attr, allow_failure=self.cache_allow_failure)
            with timed_stage("deploy: commit (total, incl. git push)"):
                await _finalize_commit(
                    deploy_branch,
                    source_branch,
                    files,
                    message,
                    prepared,
                    push=self.push,
                    remote=self.remote,
                )
        await try_jj_status(".")


class Rollback(AttrCommand):
    """Roll back the GitOps deploy (and paired source) branch to an older
    commit -- forward-only, replays the old tree as a *new* commit, never
    resets or force-pushes anything.

    Deliberately supports skipping Nix evaluation entirely via
    `--deploy-branch`/`--source-branch`: an incident is often *why* Nix
    eval is currently broken, so rollback can't depend on it working.
    `--file`/`--flake` is the routine convenience for a one-step-back
    during normal testing, when Nix eval is healthy.
    """

    deploy_branch: str | None = opt(
        None,
        help="Deploy branch to roll back, bypassing Nix evaluation entirely. Mutually exclusive with --file/--flake.",
    )
    source_branch: str | None = opt(
        None,
        help="Paired source branch to roll back alongside --deploy-branch (optional).",
    )
    to: str | None = opt(None, help="Roll back to this specific commit-ish instead of walking --steps-back.")
    steps_back: int = opt(1, help="Number of deploy-branch first-parent steps to roll back, when --to is not given.")
    push: bool = opt(False, help="git push the rolled-back branch(es) to their remote afterwards.")
    remote: str = opt("origin", help="Remote to push to (with --push).")
    verify: bool = opt(
        False,
        help="Run Validate against the restored tree before finalizing -- requires --file/--flake. "
        "Off by default: an incident rollback should be fast, and the restored tree was already "
        "validated when it was first deployed.",
    )

    async def run(self) -> None:
        has_nix_entrypoint = self.file is not None or self.flake is not None
        if self.deploy_branch is not None:
            if has_nix_entrypoint:
                _log.error("--deploy-branch is mutually exclusive with --file/--flake")
                raise SystemExit(1)
            deploy_branch, source_branch = self.deploy_branch, self.source_branch
        elif has_nix_entrypoint:
            result = await _evaluate_gitops(self.file, self.flake, self.attr)
            deploy_branch, source_branch = gitops_branches(result)
        else:
            _log.error("specify --deploy-branch, or --file/--flake to read branches from Nix")
            raise SystemExit(1)

        if self.verify and not has_nix_entrypoint:
            _log.error("--verify requires --file/--flake")
            raise SystemExit(1)

        try:
            prepared = rollback_branches(
                ".",
                deploy_branch,
                source_branch,
                steps_back=self.steps_back,
                to=self.to,
            )
        except (ValueError, TypeError, KeyError) as exc:
            # pygit2.Repository.revparse_single (called by rollback_branches
            # for --to) raises KeyError, not ValueError/TypeError, for a
            # revision spec it can't resolve.
            _log.error("rollback failed: %s", exc)
            raise SystemExit(1) from exc

        if self.verify:
            await Validate.run(cast("Validate", self))

        try:
            finalize_branches(".", deploy_branch, source_branch, prepared)
        except ConcurrentDeployError as exc:
            _log.error(str(exc))
            raise SystemExit(1) from exc
        _log.info("rolled back %s @ %s", deploy_branch, prepared.deploy_oid)

        if self.push:
            await _git_push(self.remote, deploy_branch, source_branch)
        await try_jj_status(".")


class KubeApply(AttrCommand):
    """Apply Kubernetes objects directly against the current kubeconfig
    context: server-side apply in barrier order, with optional pruning.

    General-purpose primitive backing both one-time direct bootstraps
    (`--target bootstrap` -- gets a GitOps engine running before it can
    sync itself) and `ekn validate`'s ephemeral-apiserver conformance runs
    (which apply the full `kubernetes.generated` set). Decrypts any object
    carrying a `sops:` metadata block (see ekn.sops) before applying it --
    SOPS-encrypted objects flow untouched through `ekn commit`'s GitOps
    path, but a direct apply has no ArgoCD+kustomize+ksops step to do that
    decryption for it. Also ensures every `kubernetes.sopsAgeIdentities`
    entry exists as a Secret first (generating a fresh age keypair the
    first time one is missing) -- any easykubenix consumer that needs a
    SOPS-decrypting workload bootstrapped (e.g. argocd.nix's ksops
    support) declares it there instead of a bespoke bootstrap script.
    """

    cli_name = "kubeapply"

    target: str | None = opt(
        None,
        help="Apply only this GitOps target's objects (kubernetes.gitOpsTargets). Omit for the full kubernetes.generated set.",
    )
    prune: bool = opt(
        False,
        help="Delete previously-applied objects no longer present in this apply. Scoped to this apply's discriminator: the target's own with --target, otherwise ekn.discriminator.",
    )
    confirm_context: str | None = opt(
        None,
        help="Prompt for confirmation unless the current kubectl context ends with this name.",
    )

    async def run(self) -> None:
        if self.confirm_context is not None:
            rc, out, _ = await exec_capture("kubectl", "config", "current-context")
            current = out.strip()
            if rc != 0 or not current.endswith(self.confirm_context):
                _log.warning(f"current kubectl context is {current!r}, not *{self.confirm_context}")
                answer = await anyio.to_thread.run_sync(input, "Continue anyway? [y/N] ")
                if answer.strip().lower() != "y":
                    raise SystemExit("Aborted.")

        uri, customer = _parse_flake(self.flake) if self.flake is not None else (None, None)
        try:
            cfg = await evaluate_kubeapply_config(self.file, uri, customer, self.attr, self.target)
        except NixError as exc:
            _report_nix_error(exc)
        except ValidationError as exc:
            _report_validation_error("kubeapply config", exc)
        api = await kr8s.asyncio.api()
        if cfg.sops_age_identities:
            await ensure_age_identities(cfg.sops_age_identities, api=api)
        objects = [await maybe_decrypt(obj) for obj in cfg.objects]
        try:
            await apply_and_prune(
                objects,
                api=api,
                discriminator=cfg.discriminator,
                field_manager=cfg.field_manager,
                resource_priority=cfg.resource_priority,
                prune=self.prune,
            )
        except kr8s.ServerError as exc:
            _report_server_error("apply", exc)


class ClusterDiff(AttrCommand):
    """Diff Kubernetes objects against the live cluster.

    Unlike `ekn diff` (which compares against the previous GitOps commit),
    this compares against the cluster's actual current state -- for each
    object, a server-side-apply dry run shows what `ekn kubeapply`/
    `ekn validate` would really change right now, including drift from
    manual kubectl edits or other controllers. Read-only: nothing is
    applied, pruned, or waited on.
    """

    cli_name = "clusterdiff"

    target: str | None = opt(
        None,
        help="Diff only this GitOps target's objects (kubernetes.gitOpsTargets). Omit for the full kubernetes.generated set.",
    )

    async def run(self) -> None:
        uri, customer = _parse_flake(self.flake) if self.flake is not None else (None, None)
        try:
            cfg = await evaluate_kubeapply_config(self.file, uri, customer, self.attr, self.target)
        except NixError as exc:
            _report_nix_error(exc)
        except ValidationError as exc:
            _report_validation_error("kubeapply config", exc)
        objects = [await maybe_decrypt(obj) for obj in cfg.objects]
        api = await kr8s.asyncio.api()
        try:
            diff_output = await cluster_diff(objects, api=api)
        except kr8s.ServerError as exc:
            _report_server_error("clusterdiff", exc)
        if diff_output:
            sys.stdout.write(diff_output)
        else:
            _log.info("no differences")


class PushCache(NixCommand):
    """Build a Nix attribute and copy its realised closure to a remote store.

    Manual/ad-hoc escape hatch (or for CI) for pushing an arbitrary
    attribute's closure -- for the routine per-deploy case, `ekn deploy`
    already does this automatically from `ekn.cacheTo`/`ekn.cachePackage`,
    no flags needed (see `Deploy`).
    """

    cli_name = "pushcache"

    attr: str = opt(
        required=True, help="Dot-separated attribute path to build and push, e.g. 'kubenix.config.kluctl.projectDir'."
    )
    to: str = opt(required=True, help="Destination store URI, e.g. ssh-ng://nix@host:2222")
    substitute_on_destination: bool = opt(
        True,
        negatable=True,
        help="Let the destination substitute from its own configured caches instead of streaming everything.",
    )
    check_sigs: bool = opt(
        False,
        help="Verify signatures when copying (off by default, matching kluctl's existing preDeployScript).",
    )

    async def run(self) -> None:
        await _push_cache(
            self.attr,
            self.file,
            self.flake,
            self.to,
            substitute_on_destination=self.substitute_on_destination,
            check_sigs=self.check_sigs,
        )


class SplitManifest(Command):
    """Split a JSON manifest list into a namespace/kind/name.yaml directory tree.

    Internal: used by easykubenix's `manifestYAMLDir` derivation so the whole
    GitOps tree renders as a single build instead of one derivation per
    object. Not intended for interactive use.
    """

    json_file: _Path = pos(help="JSON file holding the manifest list.")
    out_dir: _Path = pos(help="Directory to write the namespace/kind/name.yaml tree into.")

    async def run(self) -> None:
        data: JsonValue = json.loads(await Path(str(self.json_file)).read_text())
        if not isinstance(data, list):
            _log.error("expected a JSON list, got %s", type(data).__name__)
            raise SystemExit(1)
        out_dir = Path(str(self.out_dir))
        for rel_path, content in flatten_manifests(data):
            dest = out_dir / rel_path
            await dest.parent.mkdir(parents=True, exist_ok=True)
            await dest.write_text(content)


class ApplyManifest(Command):
    """Apply an already-evaluated manifest JSON file against $KUBECONFIG.

    Internal: the apply step of easykubenix's `validation.script`, which
    boots a throwaway etcd+kube-apiserver in Nix and needs manifests on it
    before it can dump an OpenAPI schema for kubeconform. That step used to
    shell out to `kluctl deploy`, which meant the gate proved a deploy path
    nothing else in the project uses; this runs the same `apply_and_prune`
    that `ekn kubeapply` (bootstrap) and `ekn validate` do.

    Takes its inputs as files rather than re-evaluating Nix, because the
    caller is a derivation-built script that already has them -- there is no
    source tree to point `-f` at from inside the store. Not intended for
    interactive use; use `ekn kubeapply` for that.
    """

    cli_name = "_applyManifest"

    manifest_file: _Path = pos(help="JSON file holding the already-evaluated manifest list.")
    discriminator: str = opt(required=True, help="Value for the ekn.dev/discriminator label (ekn.discriminator).")
    resource_priority_file: _Path | None = opt(
        None,
        help="JSON file holding ekn.resourcePriority ({kind: int}). Omitted means no barrier ordering.",
    )
    novalidate_keys_file: _Path | None = opt(
        None,
        help="JSON file holding kubernetes.novalidateKeys ({kind, namespace, name} objects) to skip.",
    )

    async def run(self) -> None:
        resource_priority: dict[str, int] = {}
        if self.resource_priority_file is not None:
            loaded = json.loads(await Path(str(self.resource_priority_file)).read_text())
            if not isinstance(loaded, dict):
                _log.error("resource priority file must hold a JSON object")
                raise SystemExit(1)
            resource_priority = cast("dict[str, int]", loaded)

        novalidate_keys: set[tuple[str, str, str]] = set()
        if self.novalidate_keys_file is not None:
            loaded_keys = json.loads(await Path(str(self.novalidate_keys_file)).read_text())
            if not isinstance(loaded_keys, list):
                _log.error("novalidate keys file must hold a JSON list")
                raise SystemExit(1)
            novalidate_keys = {
                (k["kind"], k["namespace"], k["name"]) for k in cast("list[dict[str, str]]", loaded_keys)
            }

        objects = await prepare_validation_objects(str(self.manifest_file), novalidate_keys)
        api = await kr8s.asyncio.api()
        try:
            await apply_and_prune(
                objects,
                api=api,
                discriminator=self.discriminator,
                resource_priority=resource_priority,
            )
        except kr8s.ServerError as exc:
            _report_server_error("apply", exc)


_json_value_adapter: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_json_value_list_adapter: TypeAdapter[list[JsonValue]] = TypeAdapter(list[JsonValue])

_YAML_STREAM_PARSERS: dict[str, Callable[[str], list[JsonValue]]] = {
    "yaml11": from_yaml11_stream,
    "yaml12": from_yaml_stream,
}


class YamlToJson(Command):
    """Parse a YAML document stream on stdin and dump it as a JSON array on stdout.

    Internal: the IFD-derivation fallback importyaml.nix shells out to when
    nanopynix's fromYAML11Stream/fromYAMLStream primops aren't registered
    (plain `nix build`/`nix eval`, no ekn worker attached). Reuses the exact
    same nanopynix.primops YAML-parsing code the in-process primop path
    uses, so both paths agree on YAML 1.1 vs 1.2 scalar semantics (e.g. a
    volume's `defaultMode: 0644` as octal) -- unlike the `yq`-based approach
    this replaced. Not intended for interactive use.
    """

    cli_name = "_yamlToJson"

    yaml_version: Literal["yaml11", "yaml12"] = opt(
        "yaml12",
        help="YAML version to parse the input stream with.",
    )

    async def run(self) -> None:
        source = sys.stdin.read()
        try:
            docs = _YAML_STREAM_PARSERS[self.yaml_version](source)
        except ValueError as exc:
            _log.error(str(exc))
            raise SystemExit(1) from exc
        sys.stdout.buffer.write(_json_value_list_adapter.dump_json(docs))


class JsonToYaml(Command):
    """Parse a JSON value on stdin and dump it as YAML on stdout.

    Internal: the reverse of `_yamlToJson`, reusing nanopynix's `to_yaml`
    (root lists render as a `---`-separated document stream) so a
    derivation-fallback path stays byte-for-byte consistent with the
    in-process `toYAML` primop. Not intended for interactive use.
    """

    cli_name = "_jsonToYAML"

    async def run(self) -> None:
        data = sys.stdin.buffer.read()
        try:
            value = _json_value_adapter.validate_json(data)
        except ValidationError as exc:
            _log.error(str(exc))
            raise SystemExit(1) from exc
        sys.stdout.write(to_yaml(value))


class Ekn(AttrCommand):
    """easykubenix CLI — evaluate Nix and manage GitOps release branches."""

    # **One tuple, and the order is the order `ekn --help` prints.** It used
    # to be a union annotation, because clypi read the type of a `subcommand`
    # field to find what it could mount.
    subcommands = (
        Deploy,
        Eval,
        Render,
        Diff,
        Commit,
        Rollback,
        Validate,
        KubeApply,
        ClusterDiff,
        PushCache,
        SplitManifest,
        ApplyManifest,
        YamlToJson,
        JsonToYaml,
    )

    async def run(self) -> None:
        """Evaluate the named entry point, the way `ekn eval` does.

        The root takes `--file`/`--flake` itself, so `ekn -f app.nix -A web` is
        a whole command. A caller who names neither has asked for the help.
        """
        if self.file is None and self.flake is None:
            self.print_help()
        result = await _evaluate(self.file, self.flake, self.attr)
        json.dump(result, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")


def parse(argv: Sequence[str]) -> Command:
    """The command that *argv* names, built and ready to run.

    What `main` does, without running anything. A test drives the real parser
    through this rather than a double, so a change to a declaration is a change
    the test sees.
    """
    parser = build_parser(Ekn)
    return dispatch(parser, parser.parse_args(list(argv)))


def main() -> None:
    # **The parse comes first, and the set-up after it.** A shell completion
    # and `--help` both end inside this function, and neither needs a logger or
    # a traceback handler.
    parser = build_parser(Ekn)
    complete(parser)
    command = dispatch(parser, parser.parse_args())
    rich.traceback.install(show_locals=True)
    structlog.configure(
        processors=[
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        cache_logger_on_first_use=True,
    )
    asyncio.run(command.run())
