from __future__ import annotations

import shutil
from pathlib import Path

from grpclib_transports import stdio_worker
from helmrpc_proto.helmrpc_grpc import HelmStub
from helmrpc_proto.helmrpc_pb2 import RenderRequest
from google.protobuf.struct_pb2 import Struct

_CHART_YAML = """\
apiVersion: v2
name: mychart
version: 0.1.0
"""

_CONFIGMAP_TEMPLATE = """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ .Values.name }}
  namespace: {{ .Release.Namespace }}
data:
  greeting: {{ .Values.greeting | quote }}
"""


def _write_chart(chart_dir: Path) -> None:
    chart_dir.mkdir()
    (chart_dir / "Chart.yaml").write_text(_CHART_YAML)
    templates = chart_dir / "templates"
    templates.mkdir()
    (templates / "configmap.yaml").write_text(_CONFIGMAP_TEMPLATE)


async def test_helmrpc_render_produces_real_templated_output(tmp_path: Path) -> None:
    """Render drives Helm's actual `helm template` codepath (action.Install + DryRunClient), not a stub."""
    helmrpc = shutil.which("helmrpc")
    assert helmrpc is not None, "helmrpc must be on PATH (see nix/shell.nix)"

    chart_dir = tmp_path / "mychart"
    _write_chart(chart_dir)

    values = Struct()
    values.update({"name": "my-config", "greeting": "hello from helmrpc"})

    async with stdio_worker([helmrpc]) as channel:
        stub = HelmStub(channel)
        response = await stub.Render(
            RenderRequest(
                chart_path=str(chart_dir),
                name="release-under-test",
                namespace="my-namespace",
                values=values,
            )
        )

    assert len(response.resources) == 1
    resource = response.resources[0]

    assert resource["kind"] == "ConfigMap"
    assert resource["metadata"]["name"] == "my-config"
    assert resource["metadata"]["namespace"] == "my-namespace"
    assert resource["data"]["greeting"] == "hello from helmrpc"
