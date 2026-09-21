from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
from typing import TYPE_CHECKING, Any, cast

import anyio
import anyio.abc
import structlog
from anyio import Path

from ekn.sops import maybe_decrypt

if TYPE_CHECKING:
    from types import TracebackType

    from nanopynix.models import JsonValue

    from ekn.apply import Manifest

_log = structlog.get_logger()

#: How long a child gets to answer SIGTERM before it is killed. It only has to
#: cover etcd and kube-apiserver shutting down a store nothing will read again.
TERMINATE_GRACE = 5.0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


async def exec_capture(
    *args: str,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
) -> tuple[int, str, str]:
    completed = await anyio.run_process(
        list(args),
        input=stdin.encode() if stdin else None,
        # `anyio.run_process` opens a stdin pipe only for a *truthy* `input`,
        # so an empty string would leave the child reading this process' own
        # stdin and waiting on a terminal. DEVNULL is the same EOF with no
        # pipe, and the two arguments cannot both be set.
        stdin=subprocess.DEVNULL if stdin == "" else None,
        env=env,
        check=False,
    )
    return completed.returncode, completed.stdout.decode(), completed.stderr.decode()


async def drain(stream: anyio.abc.ByteReceiveStream | None) -> str:
    """Whatever is left on *stream*, as text.

    anyio's process streams are `ByteReceiveStream`s and have no `read()`:
    the stream is an iterator of chunks, and it ends when the child closes
    its end.
    """
    if stream is None:
        return ""
    chunks = [chunk async for chunk in stream]
    return b"".join(chunks).decode(errors="replace")


async def terminate_process(process: anyio.abc.Process, grace: float = TERMINATE_GRACE) -> None:
    """Stop *process*, politely first and then not.

    **The whole body is shielded, and that is the point.** Ctrl-C reaches an
    anyio program as a cancellation, and a cancel scope re-delivers it at
    every checkpoint until the scope exits -- so an unshielded `await
    process.wait()` here would be cancelled the instant it started and leave
    etcd and kube-apiserver running with nobody holding them. The shield is
    bounded by *grace* so it cannot become a hang of its own.
    """
    with anyio.CancelScope(shield=True):
        if process.returncode is None:
            process.terminate()
            with anyio.move_on_after(grace):
                await process.wait()
        if process.returncode is None:
            process.kill()
        await process.aclose()


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


#: etcd's loopback, and deliberately not the API server's. etcd's family has no
#: bearing on the constraint that matters here -- kube-apiserver requires its
#: *advertise* address to match the first `--service-cluster-ip-range` entry,
#: and says nothing about where etcd lives. easykubenix's Nix harness
#: (validation.nix) hardcodes the same address for the same reason, and these
#: two implementations agreeing is worth more than either being clever.
ETCD_HOST = "127.0.0.1"


def bind_addresses(service_subnet: str) -> tuple[str, str]:
    """The API server's loopback for *service_subnet*, bare and bracketed.

    kube-apiserver refuses to start when its advertise address is of a
    different family from the first service CIDR:

        service IP family "fd00:96::/108" must match public address
        family "37.27.129.237"

    `serviceSubnet` lists IPv4 first (see validation.nix), so IPv6 is primary
    exactly when the first entry is an IPv6 CIDR. Dual-stack keeps IPv4.

    Two forms because they are not interchangeable: flags take the bare
    address, and a `host:port` join needs an IPv6 literal bracketed or
    everything after the first colon reads as the port.
    """
    primary = service_subnet.split(",")[0].strip()
    if ":" in primary:
        return "::1", "[::1]"
    return "127.0.0.1", "127.0.0.1"


def advertise_address(service_subnet: str) -> str:
    """The API server's advertise address for *service_subnet*.

    Not the bind address. kube-apiserver 1.37.0 refuses a loopback one:

        cannot use public IP 127.0.0.1 with endpoint reconciler:
        Invalid value: "127.0.0.1": may not be in the loopback range

    Documentation space of the primary family -- RFC 5737 for IPv4, RFC 3849
    for IPv6. Both are reserved and unroutable, and 2001:db8::/32 sits inside
    2000::/3, so it is global unicast as the check demands. validation.nix's
    `advertiseAddress` is the same pair for the same reason, and kubeadm has
    always needed it.

    Nothing binds this. It reaches only the `kubernetes` service endpoint,
    which no client here calls, and it keeps the family match `bind_addresses`
    describes.
    """
    primary = service_subnet.split(",")[0].strip()
    if ":" in primary:
        return "2001:db8::10"
    return "192.0.2.10"


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
        # Held across method calls rather than entered with `async with`: the
        # pair has to outlive `_start`, and leaving the block would close them.
        # `_teardown` is what ends them, on every path -- see `__aenter__`.
        self._etcd_proc: anyio.abc.Process | None = None
        self._apiserver_proc: anyio.abc.Process | None = None
        self.kubeconfig: str = ""
        self.schema_file: str = ""
        self.env: dict[str, str] = {}
        self._cert_dir: str = ""
        self._bind: str = ""
        self._bind_host: str = ""
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

        # Mirrors `bindAddress`/`bindHost` in validation.nix, by name as well
        # as by value: this harness exists twice, and the names lining up is
        # what makes a future change to one obviously due in the other.
        self._bind, self._bind_host = bind_addresses(self._service_subnet)
        self._advertise: str = advertise_address(self._service_subnet)
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
            # Bracketed: the only `$BIND_ADDRESS` in a kubeadm config is inside
            # `controlPlaneEndpoint`, which is a host:port join.
            .replace("$BIND_ADDRESS", self._bind_host)
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
        self._etcd_proc = await anyio.open_process(
            [
                "etcd",
                f"--data-dir={tmp}/etcd-data",
                "--name=default",
                f"--listen-client-urls=https://{ETCD_HOST}:{self._etcd_client_port}",
                f"--advertise-client-urls=https://{ETCD_HOST}:{self._etcd_client_port}",
                f"--listen-peer-urls=https://{ETCD_HOST}:{self._etcd_peer_port}",
                f"--initial-advertise-peer-urls=https://{ETCD_HOST}:{self._etcd_peer_port}",
                f"--initial-cluster=default=https://{ETCD_HOST}:{self._etcd_peer_port}",
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
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        err = ""
        for attempt in range(10):
            rc, _, err = await exec_capture(
                "etcdctl",
                f"--endpoints=https://{ETCD_HOST}:{self._etcd_client_port}",
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
            if self._etcd_proc.returncode is not None:
                _log.error(await drain(self._etcd_proc.stderr))
            raise SystemExit(1)

    async def _start_apiserver(self) -> None:
        _log.info("starting kube-apiserver")
        self._apiserver_proc = await anyio.open_process(
            [
                "kube-apiserver",
                "--watch-cache=false",
                "--anonymous-auth=false",
                f"--etcd-cafile={self._cert_dir}/etcd/ca.crt",
                f"--etcd-certfile={self._cert_dir}/apiserver-etcd-client.crt",
                f"--etcd-keyfile={self._cert_dir}/apiserver-etcd-client.key",
                f"--etcd-servers=https://{ETCD_HOST}:{self._etcd_client_port}",
                f"--service-cluster-ip-range={self._service_subnet}",
                f"--bind-address={self._bind}",
                # Not `self._bind`, and not absent. Absent, kube-apiserver
                # auto-detects the host's external address ("external host was
                # not specified, using 37.27.129.237") and dies on the family
                # mismatch however it is bound. Loopback, 1.37.0 refuses it
                # outright. `advertise_address` says what answers both.
                f"--advertise-address={self._advertise}",
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
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
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
            if self._apiserver_proc.returncode is not None:
                _log.error(await drain(self._apiserver_proc.stderr))
            raise SystemExit(1)

    async def _teardown(self) -> None:
        for proc in (self._etcd_proc, self._apiserver_proc):
            if proc is not None:
                await terminate_process(proc)
        if self._tmp is not None:
            shutil.rmtree(self._tmp, ignore_errors=True)
