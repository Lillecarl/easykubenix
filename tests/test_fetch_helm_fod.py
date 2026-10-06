from __future__ import annotations

import functools
import http.server
import io
import tarfile
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from huggorm.errors import NixError

from ekn import nix
from ekn.fod import (
    derivation_name_from_path,
    extract_fod_hash_mismatch,
    extract_unique_fod_hash_mismatch,
    find_fod_hash_literal,
    replace_fod_hash,
)

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


async def test_helm_fod_hash_mismatch_is_discovered_and_inserted(tmp_path: Path, helm_chart_url: str) -> None:
    """fetchHelm is declared with a wrong hash; the mismatch must name the real one, and the patched source must build."""
    nix_file = tmp_path / "helm-fod.nix"
    nix_file.write_text(f"""
    let
      sources = import {PROJECT_ROOT}/nix/sources.nix;
      pkgs = import sources.nixpkgs {{ }};
      fetchHelm = pkgs.callPackage {PROJECT_ROOT}/easykubenix/pkgs/fetchHelm.nix {{ }};
    in
    fetchHelm {{
      chart = "mychart";
      chartUrl = "{helm_chart_url}";
      sha256 = "{_WRONG_SHA256}";
    }}
    """)

    async with nix.evaluator() as evaluator:
        async with evaluator.capture() as logs:
            with pytest.raises(NixError) as caught:
                await (await evaluator.file(str(nix_file))).realise_string()
        mismatch = extract_fod_hash_mismatch(str(caught.value)) or extract_unique_fod_hash_mismatch(
            nix.captured_messages(logs)
        )
        assert mismatch is not None, str(caught.value)
        assert mismatch.specified == _WRONG_SHA256

        source = nix_file.read_text()
        literal = find_fod_hash_literal(
            source, mismatch.specified, derivation_name=derivation_name_from_path(mismatch.drv_path)
        )
        nix_file.write_text(replace_fod_hash(source, literal, mismatch.got))
        await evaluator.forget_files()
        out_path = Path(await (await evaluator.file(str(nix_file))).realise_string())

    assert str(out_path).startswith("/nix/store/")
    assert (out_path / "Chart.yaml").read_text() == _CHART_YAML.decode()

    patched_source = nix_file.read_text()
    assert _WRONG_SHA256 not in patched_source
    assert 'sha256 = "sha256-' in patched_source
