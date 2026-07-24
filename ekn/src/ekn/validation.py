from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import tempfile
from typing import TYPE_CHECKING, Any, cast

import anyio
import structlog
from anyio import Path

from ekn.sops import maybe_decrypt

if TYPE_CHECKING:
    from types import TracebackType

    from nanopynix.models import JsonValue

    from ekn.apply import Manifest

_log = structlog.get_logger()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


async def exec_capture(
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
        input=stdin.encode() if stdin is not None else None,
    )
    if proc.returncode is None:
        raise RuntimeError("subprocess returncode is unset after communicate() completed")
    return proc.returncode, stdout.decode(), stderr.decode()


async def prepare_validation_objects(
    manifest_path: str,
    novalidate_keys: set[tuple[str, str, str]],
) -> list[Manifest]:
    """Load `internal.manifestJSONFile`'s objects, drop anything covered by
    `ekn.novalidate` (see kubernetes.nix's `novalidateKeys` -- objects that
    can never be meaningfully verified against this throwaway, controller-
    less apiserver, e.g. an aggregated APIService whose backing Service/Pod
    never actually runs here), and decrypt any that carry a `sops:` block.
    """
    manifest_list: JsonValue = json.loads(await Path(manifest_path).read_text())
    unwrapped = manifest_list["items"] if isinstance(manifest_list, dict) else manifest_list
    if not isinstance(unwrapped, list):
        _log.error("internal.manifestJSONFile did not produce a list of objects")
        raise SystemExit(1)
    # internal.manifestJSONFile always contains one k8s object dict per list
    # entry -- see kubernetes.nix's `internal.nix`.
    objects = cast("list[dict[str, Any]]", unwrapped)
    if novalidate_keys:
        skipped = [
            obj
            for obj in objects
            if (obj["kind"], obj.get("metadata", {}).get("namespace", "none"), obj["metadata"]["name"])
            in novalidate_keys
        ]
        for obj in skipped:
            _log.debug(
                "skipping (novalidate)",
                kind=obj["kind"],
                namespace=obj.get("metadata", {}).get("namespace"),
                name=obj["metadata"]["name"],
            )
        objects = [
            obj
            for obj in objects
            if (obj["kind"], obj.get("metadata", {}).get("namespace", "none"), obj["metadata"]["name"])
            not in novalidate_keys
        ]
    return [await maybe_decrypt(obj) for obj in objects]


class EphemeralControlPlane:
    """Ephemeral, controller-less etcd+kube-apiserver pair for `ekn validate`.

    No controllers run against this apiserver (aggregated APIServices,
    reconciling controllers, etc. never actually work here -- see
    kubernetes.nix's `ekn.novalidate`); it exists purely so
    `apply_and_prune`+kubeconform can see manifests land on a real API
    server's admission/validation pipeline.

    Mutates process-global `os.environ` (CERT_DIR/KUBECONFIG/BIND_ADDRESS/
    KUBERNETES_PORT) and never restores it on exit, matching `ekn validate`'s
    previous (pre-extraction) behavior exactly -- each `ekn validate`
    invocation is a fresh, short-lived process anyway. `kubeadm_config`
    carries literal `$BIND_ADDRESS`/`$KUBERNETES_PORT`/`$CERT_DIR`
    placeholders (see easykubenix/validation.nix's `controlPlaneEndpoint`)
    that kubeadm itself does no substitution on -- the older fish-script
    `validationScript` substituted these via the shell before handing the
    config to kubeadm; `__aenter__` does the same substitution here.
    """

    def __init__(
        self,
        *,
        k8s_bin: str,
        etcd_bin: str,
        kubeconform_bin: str,
        service_subnet: str,
        kubeadm_config: dict[str, Any],
    ) -> None:
        self._k8s_bin = k8s_bin
        self._etcd_bin = etcd_bin
        self._kubeconform_bin = kubeconform_bin
        self._service_subnet = service_subnet
        self._kubeadm_config = kubeadm_config
        self._tmp: Path | None = None
        self._etcd_proc: asyncio.subprocess.Process | None = None
        self._apiserver_proc: asyncio.subprocess.Process | None = None
        self.kubeconfig: str = ""
        self.schema_file: str = ""
        self.env: dict[str, str] = {}
        self._cert_dir: str = ""
        self._bind: str = ""
        self._k8s_port: int = 0
        self._etcd_client_port: int = 0
        self._etcd_peer_port: int = 0

    async def __aenter__(self) -> EphemeralControlPlane:
        tmp = Path(tempfile.mkdtemp(suffix="eknvalidation"))
        self._tmp = tmp
        # __aexit__ is never called if __aenter__ raises, so any partial
        # startup (e.g. etcd up, apiserver health-check failing) must tear
        # itself down here rather than relying on the context manager protocol.
        try:
            await self._start(tmp)
        except BaseException:
            await self._teardown()
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._teardown()

    async def _start(self, tmp: Path) -> None:
        await self._write_certs_and_config(tmp)
        await self._start_etcd(tmp)
        await self._start_apiserver()

    async def _write_certs_and_config(self, tmp: Path) -> None:
        self._cert_dir = str(tmp / "pki")
        self.kubeconfig = str(tmp / "admin.conf")
        kubeadm_cfg = str(tmp / "kubeadm-config.json")
        self.schema_file = str(tmp / "k8s-schema.json")

        self._bind = "127.0.0.1"
        self._k8s_port = _free_port()
        self._etcd_client_port = _free_port()
        self._etcd_peer_port = _free_port()

        os.environ["CERT_DIR"] = self._cert_dir
        os.environ["KUBECONFIG"] = self.kubeconfig
        os.environ["BIND_ADDRESS"] = self._bind
        os.environ["KUBERNETES_PORT"] = str(self._k8s_port)

        await Path(self._cert_dir).mkdir(parents=True)
        kubeadm_config_text = (
            json.dumps(self._kubeadm_config)
            .replace("$BIND_ADDRESS", self._bind)
            .replace("$KUBERNETES_PORT", str(self._k8s_port))
            .replace("$CERT_DIR", self._cert_dir)
        )
        await Path(kubeadm_cfg).write_text(kubeadm_config_text)

        self.env = os.environ | {
            "PATH": f"{self._k8s_bin}:{self._etcd_bin}:{self._kubeconform_bin}:" + os.environ.get("PATH", ""),
        }

        rc, _, err = await exec_capture(
            "kubeadm",
            "init",
            "phase",
            "certs",
            "all",
            f"--config={kubeadm_cfg}",
            env=self.env,
        )
        if rc != 0:
            _log.error("kubeadm certs phase failed\n%s", err)
            raise SystemExit(1)

        rc, _, err = await exec_capture(
            "kubeadm",
            "init",
            "phase",
            "kubeconfig",
            "admin",
            f"--config={kubeadm_cfg}",
            f"--kubeconfig-dir={tmp}",
            env=self.env,
        )
        if rc != 0:
            _log.error("kubeadm kubeconfig phase failed\n%s", err)
            raise SystemExit(1)

    async def _start_etcd(self, tmp: Path) -> None:
        _log.info("starting etcd")
        self._etcd_proc = await asyncio.create_subprocess_exec(
            *[
                "etcd",
                f"--data-dir={tmp}/etcd-data",
                "--name=default",
                f"--listen-client-urls=https://{self._bind}:{self._etcd_client_port}",
                f"--advertise-client-urls=https://{self._bind}:{self._etcd_client_port}",
                f"--listen-peer-urls=https://{self._bind}:{self._etcd_peer_port}",
                f"--initial-advertise-peer-urls=https://{self._bind}:{self._etcd_peer_port}",
                f"--initial-cluster=default=https://{self._bind}:{self._etcd_peer_port}",
                "--client-cert-auth=true",
                f"--trusted-ca-file={self._cert_dir}/etcd/ca.crt",
                f"--cert-file={self._cert_dir}/etcd/server.crt",
                f"--key-file={self._cert_dir}/etcd/server.key",
                "--peer-client-cert-auth=true",
                f"--peer-trusted-ca-file={self._cert_dir}/etcd/ca.crt",
                f"--peer-cert-file={self._cert_dir}/etcd/peer.crt",
                f"--peer-key-file={self._cert_dir}/etcd/peer.key",
                "--log-level=error",
            ],
            env=self.env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        err = ""
        for attempt in range(10):
            rc, _, err = await exec_capture(
                "etcdctl",
                f"--endpoints=https://{self._bind}:{self._etcd_client_port}",
                f"--cacert={self._cert_dir}/etcd/ca.crt",
                f"--cert={self._cert_dir}/etcd/healthcheck-client.crt",
                f"--key={self._cert_dir}/etcd/healthcheck-client.key",
                "endpoint",
                "health",
                env=self.env,
            )
            if rc == 0:
                break
            await anyio.sleep(attempt * 0.5)
        else:
            _log.error("etcd failed to start\n%s", err)
            if self._etcd_proc.returncode is not None and self._etcd_proc.stderr is not None:
                etcd_err = await self._etcd_proc.stderr.read()
                _log.error(etcd_err.decode())
            raise SystemExit(1)

    async def _start_apiserver(self) -> None:
        _log.info("starting kube-apiserver")
        self._apiserver_proc = await asyncio.create_subprocess_exec(
            *[
                "kube-apiserver",
                "--watch-cache=false",
                "--anonymous-auth=false",
                f"--etcd-cafile={self._cert_dir}/etcd/ca.crt",
                f"--etcd-certfile={self._cert_dir}/apiserver-etcd-client.crt",
                f"--etcd-keyfile={self._cert_dir}/apiserver-etcd-client.key",
                f"--etcd-servers=https://{self._bind}:{self._etcd_client_port}",
                f"--service-cluster-ip-range={self._service_subnet}",
                f"--bind-address={self._bind}",
                f"--secure-port={self._k8s_port}",
                "--allow-privileged=true",
                f"--client-ca-file={self._cert_dir}/ca.crt",
                f"--kubelet-client-certificate={self._cert_dir}/apiserver-kubelet-client.crt",
                f"--kubelet-client-key={self._cert_dir}/apiserver-kubelet-client.key",
                "--service-account-issuer=https://kubernetes.default.svc.cluster.local",
                f"--service-account-key-file={self._cert_dir}/sa.pub",
                f"--service-account-signing-key-file={self._cert_dir}/sa.key",
                f"--tls-cert-file={self._cert_dir}/apiserver.crt",
                f"--tls-private-key-file={self._cert_dir}/apiserver.key",
            ],
            env=self.env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        err = ""
        for attempt in range(10):
            rc, _, err = await exec_capture(
                "kubectl",
                "get",
                "--raw",
                "/healthz",
                env=self.env,
            )
            if rc == 0:
                break
            await anyio.sleep(attempt * 0.5)
        else:
            _log.error("kube-apiserver failed to start\n%s", err)
            if self._apiserver_proc.returncode is not None and self._apiserver_proc.stderr is not None:
                apiserver_err = await self._apiserver_proc.stderr.read()
                _log.error(apiserver_err.decode())
            raise SystemExit(1)

    async def _teardown(self) -> None:
        for proc in (self._etcd_proc, self._apiserver_proc):
            if proc and proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except TimeoutError:
                    proc.kill()
        if self._tmp is not None:
            shutil.rmtree(self._tmp, ignore_errors=True)
