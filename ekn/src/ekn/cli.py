from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path as _Path
from typing import Any, Literal, cast

import kr8s.asyncio
import rich.traceback
import structlog
from anyio import Path
from clypi import Command, Positional, arg
from nanopynix import NixError
from nanopynix.models import JsonValue
from nanopynix.primops import from_yaml11_stream, from_yaml_stream, to_yaml
from pydantic import TypeAdapter, ValidationError

from ekn.apply import apply_and_prune
from ekn.eval import (
    evaluate_file,
    evaluate_flake,
    evaluate_flake_ekn,
    evaluate_generated_manifests,
    evaluate_gitops_manifests,
    evaluate_kubeapply_config,
    evaluate_validation_config,
    evaluate_validation_file,
)
from ekn.git import commit_manifests, diff_manifests, flatten_manifests, try_jj_status
from ekn.gitops import GitOpsTargetError, resolved_targets
from ekn.sops import maybe_decrypt

_log = structlog.get_logger()


async def _exec(
    *args: str,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await proc.communicate(
        input=stdin.encode() if stdin is not None else None
    )
    assert proc.returncode is not None
    return proc.returncode, stdout.decode(), stderr.decode()


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
        _log.error(exc.msg_without_ansi)
        raise SystemExit(1) from exc


async def _evaluate_gitops(
    file: _Path | None,
    flake: str | None,
    attr: str | None,
) -> dict:
    """Like `_evaluate`, but only forces kubernetes.gitopsTargets -- the
    only field Diff/Commit/Deploy read via `_gitops_file_groups`."""
    try:
        uri, customer = _parse_flake(flake) if flake is not None else (None, None)
        return await evaluate_gitops_manifests(file, uri, customer, attr)
    except NixError as exc:
        _log.error(exc.msg_without_ansi)
        raise SystemExit(1) from exc


def _dig(data: object, *keys: str) -> Any:
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _check_gitops(result: dict[str, Any]) -> str:
    enabled = _dig(result, "config", "gitops", "enable")
    if enabled is not True:
        _log.error("gitops is not enabled for this config (config.gitops.enable != true)")
        raise SystemExit(1)
    branch = _dig(result, "config", "gitops", "branch")
    if not isinstance(branch, str) or not branch:
        _log.error("config.gitops.branch is not set in the evaluated config")
        raise SystemExit(1)
    return branch


def _gitops_path(result: dict[str, Any]) -> str:
    path = _dig(result, "config", "gitops", "path")
    if path is None:
        return "./"
    if not isinstance(path, str) or not path:
        _log.error("config.gitops.path must be a non-empty string")
        raise SystemExit(1)
    return path


def _gitops_file_groups(result: dict[str, Any]) -> dict[str, list[tuple[str, str]]]:
    gitops_targets = _dig(result, "config", "kubernetes", "gitopsTargets")
    if not isinstance(gitops_targets, dict) or not gitops_targets:
        raise GitOpsTargetError("no GitOps-routed Kubernetes objects found")

    routed = resolved_targets(gitops_targets)

    groups: dict[str, dict[str, str]] = {}
    for target, target_manifests in routed.items():
        branch_files = groups.setdefault(target.branch, {})
        for path, content in flatten_manifests(target_manifests, target.path, kustomize=True):
            existing = branch_files.get(path)
            if existing is not None and existing != content:
                raise GitOpsTargetError(
                    f"conflicting generated content for {target.branch}:{path}"
                )
            branch_files[path] = content
    return {branch: list(files.items()) for branch, files in groups.items()}


class Eval(Command):
    """Evaluate Nix and dump JSON."""
    file: _Path | None = arg(None, short="f", inherited=True)
    flake: str | None = arg(None, inherited=True)
    attr: str | None = arg(None, short="A", help="Dot-separated attribute path within the evaluation result.")

    async def run(self) -> None:
        result = await _evaluate(self.file, self.flake, self.attr)
        json.dump(result, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")


class Render(Command):
    """Render Kubernetes manifests as YAML on stdout."""
    file: _Path | None = arg(None, short="f", inherited=True)
    flake: str | None = arg(None, inherited=True)
    attr: str | None = arg(None, short="A", help="Dot-separated attribute path within the evaluation result.")

    async def run(self) -> None:
        uri, customer = _parse_flake(self.flake) if self.flake is not None else (None, None)
        try:
            manifests = await evaluate_generated_manifests(self.file, uri, customer, self.attr)
        except NixError as exc:
            _log.error(exc.msg_without_ansi)
            raise SystemExit(1) from exc
        if not isinstance(manifests, list):
            _log.error("expected a list result, got %s", type(manifests).__name__)
            raise SystemExit(1)
        for _, content in flatten_manifests(manifests):
            sys.stdout.write("---\n")
            sys.stdout.write(content)


class Diff(Command):
    """Diff GitOps-routed manifests against their target branches."""
    file: _Path | None = arg(None, short="f", inherited=True)
    flake: str | None = arg(None, inherited=True)
    attr: str | None = arg(None, short="A", help="Dot-separated attribute path within the evaluation result.")

    async def run(self) -> None:
        result = await _evaluate_gitops(self.file, self.flake, self.attr)
        try:
            groups = _gitops_file_groups(result)
        except (GitOpsTargetError, TypeError) as exc:
            _log.error("GitOps routing failed: %s", exc)
            raise SystemExit(1) from exc
        has_differences = False
        for branch, files in groups.items():
            try:
                diff_output = diff_manifests(".", branch, files)
            except Exception as exc:
                _log.error("diff failed: %s", exc)
                raise SystemExit(1) from exc
            if diff_output is not None:
                has_differences = True
                sys.stdout.write(diff_output)
        if not has_differences:
            _log.info("no differences")
        await try_jj_status(".")


class Commit(Command):
    """Render manifests and write them to their GitOps target branches."""
    file: _Path | None = arg(None, short="f", inherited=True)
    flake: str | None = arg(None, inherited=True)
    attr: str | None = arg(None, short="A", help="Dot-separated attribute path within the evaluation result.")
    message: str | None = arg(None, short="m", help="Commit message.")

    async def run(self) -> None:
        result = await _evaluate_gitops(self.file, self.flake, self.attr)
        try:
            groups = _gitops_file_groups(result)
        except (GitOpsTargetError, TypeError) as exc:
            _log.error("GitOps routing failed: %s", exc)
            raise SystemExit(1) from exc
        if not self.message:
            import pygit2
            try:
                repo = pygit2.Repository(".")
                head_sha = str(repo.head.target)[:7]
            except Exception:
                head_sha = "unknown"
            self.message = f"ekn: render manifests from {self.attr or 'root'} @ {head_sha}"
        for branch, files in groups.items():
            try:
                commit_id = commit_manifests(".", branch, files, self.message)
            except Exception as exc:
                _log.error("commit failed: %s", exc)
                raise SystemExit(1) from exc
            _log.info("committed to %s @ %s", branch, commit_id)
        await try_jj_status(".")


class Validate(Command):
    """Boot real etcd+kube-apiserver, apply manifests, and run kubeconform."""
    file: _Path | None = arg(None, short="f", inherited=True)
    flake: str | None = arg(None, inherited=True)
    attr: str | None = arg(None, short="A", help="Dot-separated attribute path within the evaluation result.")

    @staticmethod
    def _free_port() -> int:
        import socket
        with socket.socket() as s:
            s.bind(("", 0))
            return s.getsockname()[1]

    async def run(self) -> None:
        if self.file is not None:
            cfg = await evaluate_validation_file(self.file, self.attr)
        else:
            uri, customer = _parse_flake(str(self.flake))
            if customer is None:
                _log.error("--flake must include a customer attr (e.g. '.#myapp')")
                raise SystemExit(1)
            cfg = await evaluate_validation_config(uri, customer)
        c = cfg["config"]

        tmp = Path(tempfile.mkdtemp(suffix="eknvalidation"))
        cert_dir = str(tmp / "pki")
        kubeconfig = str(tmp / "admin.conf")
        kubeadm_cfg = str(tmp / "kubeadm-config.json")
        schema_file = str(tmp / "k8s-schema.json")

        k8s_bin = c["kubernetes"]["package"]["outPath"] + "/bin"
        etcd_bin = c["validation"]["etcdPackage"]["outPath"] + "/bin"
        kubeconform_bin = c["validation"]["kubeconformPackage"]["outPath"] + "/bin"
        manifest_path = c["internal"]["manifestJSONFile"]["outPath"]

        subnet = c["validation"]["serviceSubnet"]
        bind = "127.0.0.1"
        k8s_port = self._free_port()
        etcd_client_port = self._free_port()
        etcd_peer_port = self._free_port()

        os.environ["CERT_DIR"] = cert_dir
        os.environ["KUBECONFIG"] = kubeconfig
        os.environ["BIND_ADDRESS"] = bind
        os.environ["KUBERNETES_PORT"] = str(k8s_port)

        etcd_proc: asyncio.subprocess.Process | None = None
        apiserver_proc: asyncio.subprocess.Process | None = None

        try:
            await Path(cert_dir).mkdir(parents=True)
            # kubeadmConfig carries literal $BIND_ADDRESS/$KUBERNETES_PORT
            # placeholders (see easykubenix/validation.nix's
            # controlPlaneEndpoint) -- the older fish-script validationScript
            # substituted these via the shell before handing the config to
            # kubeadm; kubeadm itself does no env-var expansion on its config
            # files, so do the same substitution here.
            kubeadm_config_text = (
                json.dumps(c["validation"]["kubeadmConfig"])
                .replace("$BIND_ADDRESS", bind)
                .replace("$KUBERNETES_PORT", str(k8s_port))
                .replace("$CERT_DIR", cert_dir)
            )
            await Path(kubeadm_cfg).write_text(kubeadm_config_text)

            env = os.environ | {
                "PATH": f"{k8s_bin}:{etcd_bin}:{kubeconform_bin}:" + os.environ.get("PATH", "")
            }

            rc, _, err = await _exec(
                "kubeadm", "init", "phase", "certs", "all", f"--config={kubeadm_cfg}",
                env=env,
            )
            if rc != 0:
                _log.error("kubeadm certs phase failed\n%s", err)
                raise SystemExit(1)

            rc, _, err = await _exec(
                "kubeadm", "init", "phase", "kubeconfig", "admin",
                f"--config={kubeadm_cfg}", f"--kubeconfig-dir={tmp}",
                env=env,
            )
            if rc != 0:
                _log.error("kubeadm kubeconfig phase failed\n%s", err)
                raise SystemExit(1)

            rc, _, err = await _exec(
                "kubeadm", "init", "phase", "kubeconfig", "admin",
                f"--config={kubeadm_cfg}", f"--kubeconfig-dir={tmp}",
                env=env,
            )
            if rc != 0:
                _log.error("kubeadm kubeconfig phase failed\n%s", err)
                raise SystemExit(1)

            _log.info("starting etcd")
            etcd_proc = await asyncio.create_subprocess_exec(
                *["etcd",
                  f"--data-dir={tmp}/etcd-data",
                  "--name=default",
                  f"--listen-client-urls=https://{bind}:{etcd_client_port}",
                  f"--advertise-client-urls=https://{bind}:{etcd_client_port}",
                  f"--listen-peer-urls=https://{bind}:{etcd_peer_port}",
                  f"--initial-advertise-peer-urls=https://{bind}:{etcd_peer_port}",
                  f"--initial-cluster=default=https://{bind}:{etcd_peer_port}",
                  "--client-cert-auth=true",
                  f"--trusted-ca-file={cert_dir}/etcd/ca.crt",
                  f"--cert-file={cert_dir}/etcd/server.crt",
                  f"--key-file={cert_dir}/etcd/server.key",
                  "--peer-client-cert-auth=true",
                  f"--peer-trusted-ca-file={cert_dir}/etcd/ca.crt",
                  f"--peer-cert-file={cert_dir}/etcd/peer.crt",
                  f"--peer-key-file={cert_dir}/etcd/peer.key",
                  "--log-level=error"],
                env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )

            for attempt in range(10):
                rc, _, err = await _exec(
                    "etcdctl",
                    f"--endpoints=https://{bind}:{etcd_client_port}",
                    f"--cacert={cert_dir}/etcd/ca.crt",
                    f"--cert={cert_dir}/etcd/healthcheck-client.crt",
                    f"--key={cert_dir}/etcd/healthcheck-client.key",
                    "endpoint", "health",
                    env=env,
                )
                if rc == 0:
                    break
                await asyncio.sleep(attempt * 0.5)
            else:
                _log.error("etcd failed to start\n%s", err)
                if etcd_proc.returncode is not None and etcd_proc.stderr is not None:
                    etcd_err = await etcd_proc.stderr.read()
                    _log.error(etcd_err.decode())
                raise SystemExit(1)

            _log.info("starting kube-apiserver")
            apiserver_proc = await asyncio.create_subprocess_exec(
                *["kube-apiserver",
                  "--watch-cache=false",
                  "--anonymous-auth=false",
                  f"--etcd-cafile={cert_dir}/etcd/ca.crt",
                  f"--etcd-certfile={cert_dir}/apiserver-etcd-client.crt",
                  f"--etcd-keyfile={cert_dir}/apiserver-etcd-client.key",
                  f"--etcd-servers=https://{bind}:{etcd_client_port}",
                  f"--service-cluster-ip-range={subnet}",
                  f"--bind-address={bind}",
                  f"--secure-port={k8s_port}",
                  "--allow-privileged=true",
                  f"--client-ca-file={cert_dir}/ca.crt",
                  f"--kubelet-client-certificate={cert_dir}/apiserver-kubelet-client.crt",
                  f"--kubelet-client-key={cert_dir}/apiserver-kubelet-client.key",
                  "--service-account-issuer=https://kubernetes.default.svc.cluster.local",
                  f"--service-account-key-file={cert_dir}/sa.pub",
                  f"--service-account-signing-key-file={cert_dir}/sa.key",
                  f"--tls-cert-file={cert_dir}/apiserver.crt",
                  f"--tls-private-key-file={cert_dir}/apiserver.key"],
                env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )

            for attempt in range(10):
                rc, _, err = await _exec(
                    "kubectl", "get", "--raw", "/healthz",
                    env=env,
                )
                if rc == 0:
                    break
                await asyncio.sleep(attempt * 0.5)
            else:
                _log.error("kube-apiserver failed to start\n%s", err)
                if apiserver_proc.returncode is not None and apiserver_proc.stderr is not None:
                    apiserver_err = await apiserver_proc.stderr.read()
                    _log.error(apiserver_err.decode())
                raise SystemExit(1)

            _log.info("applying manifests")
            manifest_list = json.loads(await Path(manifest_path).read_text())
            objects = manifest_list["items"] if isinstance(manifest_list, dict) else manifest_list
            objects = [await maybe_decrypt(obj) for obj in objects]
            kr8s_api = await kr8s.asyncio.api(kubeconfig=kubeconfig)
            try:
                await apply_and_prune(
                    objects,
                    api=kr8s_api,
                    discriminator=c["kluctl"]["discriminator"],
                    resource_priority=c["kluctl"]["resourcePriority"],
                )
            except kr8s.ServerError as exc:
                # kr8s only extracts the JSON `message` field for 4xx errors
                # (see kr8s._api.Api.call_api) -- for 5xx it falls back to
                # str(httpx exception), which omits the API server's actual
                # response body. Surface it ourselves since that body is
                # usually the only clue for a 500.
                body = exc.response.text if exc.response is not None else None
                _log.error("apply failed\n%s\nresponse body: %s", exc, body)
                raise SystemExit(1) from exc
            except Exception as exc:
                _log.error("apply failed\n%s", exc)
                raise SystemExit(1) from exc

            _log.info("dumping OpenAPI schema")
            rc, out, err = await _exec(
                "kubectl", "get", "--raw", "/openapi/v2",
                env=env,
            )
            if rc != 0:
                _log.error("OpenAPI schema dump failed\n%s", err)
                raise SystemExit(1)
            await Path(schema_file).write_text(out)

            _log.info("running kubeconform")
            manifest_data = await Path(manifest_path).read_text()
            rc, out, err = await _exec(
                "kubeconform", f"-schema-location={schema_file}", "-summary",
                stdin=manifest_data, env=env,
            )
            sys.stdout.write(out)
            if rc != 0:
                _log.error("%s\nkubeconform verification failed", err)
                raise SystemExit(1)

            _log.info("Your manifests are as valid as they can be against Kubernetes %s", c["kubernetes"]["package"]["version"])

        finally:
            for proc in (etcd_proc, apiserver_proc):
                if proc and proc.returncode is None:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=5)
                    except TimeoutError:
                        proc.kill()
            shutil.rmtree(tmp, ignore_errors=True)


class Deploy(Commit):
    """Verify and write GitOps-routed manifests to their target branches."""

    no_verify: bool = arg(
        False,
        help="Skip temporary API-server and kubeconform verification.",
    )
    _free_port = staticmethod(Validate._free_port)

    async def run(self) -> None:
        if not self.no_verify:
            await Validate.run(cast(Validate, self))
        await super().run()


class KubeApply(Command):
    """Apply Kubernetes objects directly against the current kubeconfig
    context: server-side apply in barrier order, with optional pruning.

    General-purpose primitive backing both scripts/bootstrap-argocd.py
    (`--target bootstrap`, one-time -- gets ArgoCD running before it can
    sync itself) and `ekn validate`'s ephemeral-apiserver conformance runs
    (which apply the full `kubernetes.generated` set). Decrypts any object
    carrying a `sops:` metadata block (see ekn.sops) before applying it --
    SOPS-encrypted objects flow untouched through `ekn commit`'s GitOps
    path, but a direct apply has no ArgoCD+kustomize+ksops step to do that
    decryption for it.
    """
    @classmethod
    def prog(cls) -> str:
        return "kubeapply"

    file: _Path | None = arg(None, short="f", inherited=True)
    flake: str | None = arg(None, inherited=True)
    attr: str | None = arg(None, short="A", help="Dot-separated attribute path within the evaluation result.")
    target: str | None = arg(
        None,
        help="Apply only this GitOps target's objects (kubernetes.gitopsTargets). Omit for the full kubernetes.generated set.",
    )
    prune: bool = arg(
        False,
        help="Delete previously-applied (same discriminator) objects no longer present in this apply.",
    )

    async def run(self) -> None:
        uri, customer = _parse_flake(self.flake) if self.flake is not None else (None, None)
        try:
            cfg = await evaluate_kubeapply_config(self.file, uri, customer, self.attr, self.target)
        except NixError as exc:
            _log.error(exc.msg_without_ansi)
            raise SystemExit(1) from exc
        objects = [await maybe_decrypt(obj) for obj in cfg["objects"]]
        api = await kr8s.asyncio.api()
        try:
            await apply_and_prune(
                objects,
                api=api,
                discriminator=cfg["discriminator"],
                resource_priority=cfg["resource_priority"],
                prune=self.prune,
            )
        except kr8s.ServerError as exc:
            body = exc.response.text if exc.response is not None else None
            _log.error("apply failed\n%s\nresponse body: %s", exc, body)
            raise SystemExit(1) from exc


class SplitManifest(Command):
    """Split a JSON manifest list into a namespace/kind/name.yaml directory tree.

    Internal: used by easykubenix's `manifestYAMLDir` derivation so the whole
    GitOps tree renders as a single build instead of one derivation per
    object. Not intended for interactive use.
    """
    json_file: Positional[_Path]
    out_dir: Positional[_Path]

    async def run(self) -> None:
        data = json.loads(await Path(str(self.json_file)).read_text())
        if not isinstance(data, list):
            _log.error("expected a JSON list, got %s", type(data).__name__)
            raise SystemExit(1)
        out_dir = Path(str(self.out_dir))
        for rel_path, content in flatten_manifests(data):
            dest = out_dir / rel_path
            await dest.parent.mkdir(parents=True, exist_ok=True)
            await dest.write_text(content)


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
    yaml_version: Literal["yaml11", "yaml12"] = arg(
        "yaml12", help="YAML version to parse the input stream with."
    )

    @classmethod
    def prog(cls) -> str:
        return "_yamlToJson"

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

    @classmethod
    def prog(cls) -> str:
        return "_jsonToYAML"

    async def run(self) -> None:
        data = sys.stdin.buffer.read()
        try:
            value = _json_value_adapter.validate_json(data)
        except ValidationError as exc:
            _log.error(str(exc))
            raise SystemExit(1) from exc
        sys.stdout.write(to_yaml(value))


class Ekn(Command):
    """easykubenix CLI — evaluate Nix and manage GitOps release branches."""
    subcommand: (
        Deploy
        | Eval
        | Render
        | Diff
        | Commit
        | Validate
        | KubeApply
        | SplitManifest
        | YamlToJson
        | JsonToYaml
        | None
    ) = None
    file: _Path | None = arg(None, short="f", help="Nix file to evaluate.")
    flake: str | None = arg(None, help="Flake reference (e.g. '.#myconfig'). Evaluates outputs.eknConfig.<system>.<attr>.")
    attr: str | None = arg(None, short="A", help="Dot-separated attribute path within the evaluation result.")

    async def run(self) -> None:
        if self.file is None and self.flake is None:
            print("Usage: ekn [OPTIONS] COMMAND [ARGS]...")
            print()
            print("  easykubenix CLI — evaluate Nix and manage GitOps release branches.")
            print()
            print("Options:")
            print("  --file, -f       Nix file to evaluate.")
            print("  --flake          Flake reference (e.g. '.#myconfig').")
            print("  --attr, -A       Dot-separated attribute path within the evaluation result.")
            print()
            print("Commands:")
            print("  deploy        Verify, then render and write routed GitOps manifests.")
            print("  eval          Evaluate Nix and dump JSON.")
            print("  render        Render Kubernetes manifests as YAML on stdout.")
            print("  diff          Diff rendered manifests against the GitOps branch.")
            print("  commit        Render manifests and write them to the GitOps branch.")
            print("  validate      Boot real etcd+kube-apiserver, apply manifests, run kubeconform.")
            print("  kubeapply     Apply Kubernetes objects directly against the current kubeconfig context.")
            raise SystemExit(0)
        result = await _evaluate(self.file, self.flake, self.attr)
        json.dump(result, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")


def main() -> None:
    rich.traceback.install(show_locals=True)
    structlog.configure(
        processors=[
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        cache_logger_on_first_use=True,
    )
    cli = Ekn.parse()
    cli.start()
