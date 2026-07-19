from __future__ import annotations

from pathlib import Path

from ekn.eval import evaluate_file

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


async def test_render_helm_primop_is_callable_from_nix(tmp_path: Path) -> None:
    """builtins.renderHelm must be usable from ordinary Nix code, not just from Python."""
    chart_dir = tmp_path / "mychart"
    _write_chart(chart_dir)

    nix_file = tmp_path / "render.nix"
    nix_file.write_text(f"""
    let
      requestJSON = builtins.toJSON {{
        chart = "{chart_dir}";
        name = "release-under-test";
        namespace = "my-namespace";
        values = {{ name = "my-config"; greeting = "hello from helmrpc"; }};
      }};
    in
    builtins.fromJSON (builtins.renderHelm requestJSON)
    """)

    resources = await evaluate_file(nix_file, None)

    assert isinstance(resources, list)
    assert len(resources) == 1
    resource = resources[0]

    assert resource["kind"] == "ConfigMap"
    assert resource["metadata"]["name"] == "my-config"
    assert resource["metadata"]["namespace"] == "my-namespace"
    assert resource["data"]["greeting"] == "hello from helmrpc"
