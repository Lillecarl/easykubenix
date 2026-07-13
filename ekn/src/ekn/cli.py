from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path as _Path
from typing import Any

import rich.traceback
import structlog
from anyio import Path
from clypi import Command, arg
from nanopynix import NixError

from ekn.eval import evaluate_file, evaluate_flake, evaluate_flake_ekn, evaluate_validation_config
from ekn.git import commit_manifests, diff_manifests, flatten_manifests, try_jj_status

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
        uri = str(Path.cwd())
    return uri, customer


async def _evaluate(
    file: _Path | None,
    flake: str | None,
    attr_path: str | None,
) -> object:
    try:
        if flake is not None:
            uri, customer = _parse_flake(flake)
            if customer:
                return await evaluate_flake_ekn(uri, customer)
            return await evaluate_flake(uri, attr_path)
        if file is not None:
            return await evaluate_file(file, attr_path)
        raise ValueError("specify --file or --flake")
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


def _get_manifests(result: dict[str, Any]) -> dict[str, Any]:
    manifests = _dig(result, "config", "kubernetes", "generatedByPath")
    if not isinstance(manifests, dict):
        _log.error("no kubernetes objects found (config.kubernetes.generatedByPath is empty or not a dict)")
        raise SystemExit(1)
    return manifests


class Eval(Command):
    """Evaluate Nix and dump JSON."""
    file: _Path | None = arg(None, short="f", inherited=True)
    flake: str | None = arg(None, inherited=True)
    attr_path: str | None = arg(None, help="Dot-path into the evaluated file or flake output.")

    async def run(self) -> None:
        result = await _evaluate(self.file, self.flake, self.attr_path)
        json.dump(result, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")


class Diff(Command):
    """Diff rendered manifests against the GitOps branch."""
    file: _Path | None = arg(None, short="f", inherited=True)
    flake: str | None = arg(None, inherited=True)
    attr_path: str | None = arg(None, help="Dot-path into the evaluated file or flake output.")
    branch: str | None = arg(None, short="b", help="Override the branch name from config.gitops.branch.")
    subdir: str | None = arg(None, short="s", help="Override the subdirectory from config.gitops.path.")

    async def run(self) -> None:
        result = await _evaluate(self.file, self.flake, self.attr_path)
        if not isinstance(result, dict):
            _log.error("expected a dict result, got %s", type(result).__name__)
            raise SystemExit(1)
        resolved_branch = self.branch or _check_gitops(result)
        resolved_subdir = self.subdir or _gitops_path(result)
        manifests = _get_manifests(result)
        try:
            files = flatten_manifests(manifests, resolved_subdir)
        except TypeError as exc:
            _log.error("error: %s", exc)
            raise SystemExit(1) from exc
        try:
            diff_output = diff_manifests(".", resolved_branch, files)
        except Exception as exc:
            _log.error("diff failed: %s", exc)
            raise SystemExit(1) from exc
        if diff_output is None:
            _log.info("no differences")
            return
        sys.stdout.write(diff_output)
        await try_jj_status(".")


class Commit(Command):
    """Render manifests and write them to the GitOps branch."""
    file: _Path | None = arg(None, short="f", inherited=True)
    flake: str | None = arg(None, inherited=True)
    attr_path: str | None = arg(None, help="Dot-path into the evaluated file or flake output.")
    branch: str | None = arg(None, short="b", help="Override the branch name from config.gitops.branch.")
    message: str | None = arg(None, short="m", help="Commit message.")
    subdir: str | None = arg(None, short="s", help="Override the subdirectory from config.gitops.path.")

    async def run(self) -> None:
        result = await _evaluate(self.file, self.flake, self.attr_path)
        if not isinstance(result, dict):
            _log.error("expected a dict result, got %s", type(result).__name__)
            raise SystemExit(1)
        resolved_branch = self.branch or _check_gitops(result)
        resolved_subdir = self.subdir or _gitops_path(result)
        manifests = _get_manifests(result)
        try:
            files = flatten_manifests(manifests, resolved_subdir)
        except TypeError as exc:
            _log.error("error: %s", exc)
            raise SystemExit(1) from exc
        if not self.message:
            import pygit2
            try:
                repo = pygit2.Repository(".")
                head_sha = str(repo.head.target)[:7]
            except Exception:
                head_sha = "unknown"
            self.message = f"ekn: render manifests from {self.attr_path or 'root'} @ {head_sha}"
        try:
            commit_id = commit_manifests(".", resolved_branch, files, self.message)
        except Exception as exc:
            _log.error("commit failed: %s", exc)
            raise SystemExit(1) from exc
        _log.info("committed to %s @ %s", resolved_branch, commit_id)
        await try_jj_status(".")


class Validate(Command):
    """Boot real etcd+kube-apiserver, apply manifests, and run kubeconform."""
    file: _Path | None = arg(None, short="f", inherited=True)
    flake: str | None = arg(None, inherited=True)
    attr_path: str | None = arg(None, help="Dot-path into the evaluated config.")

    @staticmethod
    def _free_port() -> int:
        import socket
        with socket.socket() as s:
            s.bind(("", 0))
            return s.getsockname()[1]

    async def run(self) -> None:
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
        kluctl_exe = c["kluctl"]["script"]["outPath"] + "/bin/kubenixDeploy"
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
            await Path(kubeadm_cfg).write_text(json.dumps(c["validation"]["kubeadmConfig"]))

            env = os.environ | {"PATH": f"{k8s_bin}:{etcd_bin}:" + os.environ.get("PATH", "")}

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
            rc, _, err = await _exec(kluctl_exe, "--yes", "--no-wait", env=env)
            if rc != 0:
                _log.error("kluctl deploy failed\n%s", err)
                raise SystemExit(1)

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


class Ekn(Command):
    """easykubenix CLI — evaluate Nix and manage GitOps release branches."""
    subcommand: Eval | Diff | Commit | Validate | None = None
    file: _Path | None = arg(None, short="f", help="Nix file to evaluate.")
    flake: str | None = arg(None, help="Flake reference (e.g. '.#myconfig'). Evaluates outputs.eknConfig.<system>.<attr>.")

    async def run(self) -> None:
        if self.file is None and self.flake is None:
            print("Usage: ekn [OPTIONS] COMMAND [ARGS]...")
            print()
            print("  easykubenix CLI — evaluate Nix and manage GitOps release branches.")
            print()
            print("Options:")
            print("  --file, -f    Nix file to evaluate.")
            print("  --flake       Flake reference (e.g. '.#myconfig').")
            print()
            print("Commands:")
            print("  eval          Evaluate Nix and dump JSON.")
            print("  diff          Diff rendered manifests against the GitOps branch.")
            print("  commit        Render manifests and write them to the GitOps branch.")
            print("  validate      Boot real etcd+kube-apiserver, apply manifests, run kubeconform.")
            raise SystemExit(0)
        result = await _evaluate(self.file, self.flake, None)
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
