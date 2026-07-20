from __future__ import annotations

from pathlib import Path

from ekn.eval import evaluate_file

PROJECT_ROOT = Path(__file__).resolve().parent.parent

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

# Never references .Release.Namespace -- real `helm template` renders this
# with no metadata.namespace at all, same as this fixture.
_SERVICEACCOUNT_TEMPLATE = """\
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {{ .Values.name }}-sa
"""


def _write_chart(chart_dir: Path) -> None:
    chart_dir.mkdir()
    (chart_dir / "Chart.yaml").write_text(_CHART_YAML)
    templates = chart_dir / "templates"
    templates.mkdir()
    (templates / "configmap.yaml").write_text(_CONFIGMAP_TEMPLATE)
    (templates / "serviceaccount.yaml").write_text(_SERVICEACCOUNT_TEMPLATE)


async def test_render_helm_primop_is_callable_from_nix(tmp_path: Path) -> None:
    """builtins.renderHelm must be usable from ordinary Nix code, not just from Python."""
    chart_dir = tmp_path / "mychart"
    _write_chart(chart_dir)

    nix_file = tmp_path / "render.nix"
    nix_file.write_text(f"""
    builtins.renderHelm {{
      chart = "{chart_dir}";
      name = "release-under-test";
      namespace = "my-namespace";
      values = {{ name = "my-config"; greeting = "hello from helmrpc"; }};
    }}
    """)

    by_path = await evaluate_file(nix_file, None)

    # namespace.kind.name -- the same shape kubernetes.objects uses, so the
    # result assigns straight in with no reshaping on the Nix side.
    assert set(by_path.keys()) == {"my-namespace", "none"}
    configmap = by_path["my-namespace"]["ConfigMap"]["my-config"]
    assert configmap["metadata"]["namespace"] == "my-namespace"
    assert configmap["data"]["greeting"] == "hello from helmrpc"

    # The ServiceAccount template never sets metadata.namespace -- it belongs
    # on the "none" attrpath, the same sentinel kubernetes.objects uses for
    # any other object without one.
    serviceaccount = by_path["none"]["ServiceAccount"]["my-config-sa"]
    assert serviceaccount["kind"] == "ServiceAccount"


async def test_render_helm_accepts_a_derivation_chart_path_directly(tmp_path: Path) -> None:
    """chart callers (e.g. fetchHelm) pass "${someDerivation}" -- a string with
    Nix string context. builtins.renderHelm must build that derivation and
    substitute the real store path itself; callers must NOT need the
    builtins.pathExists + unsafeDiscardStringContext dance to force it first."""
    chart_dir = tmp_path / "mychart"
    _write_chart(chart_dir)

    nix_file = tmp_path / "render.nix"
    nix_file.write_text(f"""
    let
      compat = import {PROJECT_ROOT}/nix/compat.nix;
      pkgs = import compat.inputs.nixpkgs {{ }};
      # A bare (unquoted) absolute path is a Nix path *value*, not a plain
      # string -- interpolating it below ("${{chartSrc}}") is what makes Nix
      # copy it into the store as a real build input. Embedding chart_dir
      # directly inside the builder's shell string instead (as text) doesn't:
      # the sandboxed builder can't see an arbitrary host path it was never
      # told is a dependency.
      chartSrc = {chart_dir};
      chart = pkgs.runCommand "mychart" {{ }} "cp -r ${{chartSrc}} $out";
    in
    builtins.renderHelm {{
      chart = "${{chart}}";
      name = "release-under-test";
      namespace = "my-namespace";
      values = {{ name = "my-config"; greeting = "hello from helmrpc"; }};
    }}
    """)

    by_path = await evaluate_file(nix_file, None)

    configmap = by_path["my-namespace"]["ConfigMap"]["my-config"]
    assert configmap["data"]["greeting"] == "hello from helmrpc"
