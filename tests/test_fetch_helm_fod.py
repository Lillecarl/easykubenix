from __future__ import annotations

import functools
import http.server
import io
import tarfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

import nanopynix
import pytest
from nanopynix_helpers.build import build_with_fod_update

if TYPE_CHECKING:
    from nanopynix.rpc.client import ValueProxy

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_WRONG_SHA256 = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
_CHART_YAML = b"apiVersion: v2\nname: mychart\nversion: 0.1.0\n"


def _chart_tarball() -> bytes:
    """Pack a minimal Helm chart into a tarball, as `helm package` would."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo("mychart/Chart.yaml")
        info.size = len(_CHART_YAML)
        tar.addfile(info, io.BytesIO(_CHART_YAML))
    return buffer.getvalue()


@pytest.fixture
def helm_chart_url(tmp_path: Path) -> Iterator[str]:
    """Serve a minimal Helm chart tarball over a local HTTP server, standing in for a real chart repo."""
    chart_dir = tmp_path / "chartserver"
    chart_dir.mkdir()
    (chart_dir / "mychart-0.1.0.tgz").write_bytes(_chart_tarball())

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(chart_dir))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        yield f"http://127.0.0.1:{port}/mychart-0.1.0.tgz"
    finally:
        server.shutdown()
        thread.join()


async def test_helm_fod_hash_mismatch_is_discovered_and_inserted(
    tmp_path: Path, helm_chart_url: str
) -> None:
    """fetchHelm is declared with a wrong hash; build_with_fod_update must patch in the real one."""
    nix_file = tmp_path / "helm-fod.nix"
    nix_file.write_text(f"""
    let
      compat = import {PROJECT_ROOT}/nix/compat.nix;
      pkgs = import compat.inputs.nixpkgs {{ }};
      fetchHelm = pkgs.callPackage {PROJECT_ROOT}/easykubenix/pkgs/fetchHelm.nix {{ }};
    in
    fetchHelm {{
      chart = "mychart";
      chartUrl = "{helm_chart_url}";
      sha256 = "{_WRONG_SHA256}";
    }}
    """)

    async with (
        nanopynix.Session(experimental_features=["flakes", "nix-command"]) as nix,
        nix.store("auto") as store,
        nix.eval(store) as session,
    ):

        async def evaluate() -> ValueProxy:
            return await session.file(str(nix_file))

        outputs, updates = await build_with_fod_update(
            evaluate,
            nix=nix,
            eval_session=session,
            evaluation_store=store,
            update_fod=True,
            source_file=nix_file,
        )

    assert updates == 1
    out_path = Path(outputs["out"])
    assert str(out_path).startswith("/nix/store/")
    assert (out_path / "Chart.yaml").read_text() == _CHART_YAML.decode()

    patched_source = nix_file.read_text()
    assert _WRONG_SHA256 not in patched_source
    assert 'sha256 = "sha256-' in patched_source
