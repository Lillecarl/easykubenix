from __future__ import annotations

import shutil
from typing import Any

from google.protobuf.json_format import MessageToDict
from google.protobuf.struct_pb2 import Struct
from grpclib_transports import stdio_worker
from helmrpc_proto.helmrpc_grpc import HelmStub
from helmrpc_proto.helmrpc_pb2 import RenderRequest
from nanopynix import PrimOpSpec

# nanopynix's RPC primop backchannel now carries DeepValue (recursive
# attrs/list/scalar), so the request attrset and the rendered resources cross
# as real Nix values -- no builtins.toJSON/builtins.fromJSON needed on either
# side. Field names mirror easykubenix/pkgs/chart2json.nix's (chart, name,
# namespace, values, kubeVersion, includeCRDs, noHooks, apiVersions).
RENDER_HELM_SPEC = PrimOpSpec(
    name="renderHelm",
    arity=1,
    args=["request"],
    doc=(
        "Render a Helm chart with `helm template` semantics via the helmrpc "
        "gRPC service. Takes an attrset with fields chart, name, namespace, "
        "values, kubeVersion, includeCRDs, noHooks, apiVersions and returns "
        "the rendered resources grouped `namespace.kind.name -> object`, "
        "the same shape `kubernetes.objects` uses -- assign the result "
        "straight into it (or `lib.recursiveUpdate` it in) with no "
        "reshaping on the Nix side."
    ),
    rpc=True,
)


async def render_helm(request: dict[str, Any]) -> dict[str, Any]:
    helmrpc = shutil.which("helmrpc")
    if helmrpc is None:
        raise RuntimeError("helmrpc binary not found on PATH")

    values = Struct()
    if request.get("values"):
        values.update(request["values"])

    async with stdio_worker([helmrpc]) as channel:
        stub = HelmStub(channel)
        response = await stub.Render(
            RenderRequest(
                chart_path=request["chart"],
                name=request.get("name", ""),
                namespace=request.get("namespace", ""),
                values=values,
                kube_version=request.get("kubeVersion", ""),
                include_crds=request.get("includeCRDs", False),
                no_hooks=request.get("noHooks", False),
                api_versions=request.get("apiVersions", []),
            )
        )

    by_path: dict[str, Any] = {}
    for resource in response.resources:
        obj = MessageToDict(resource)
        kind = obj["kind"]
        name = obj["metadata"]["name"]
        # "none" is the same sentinel kubernetes.nix uses for objects without
        # a namespace -- see its `${object.metadata.namespace or "none"}`.
        namespace = obj.get("metadata", {}).get("namespace") or "none"

        kind_bucket = by_path.setdefault(namespace, {}).setdefault(kind, {})
        if name in kind_bucket:
            raise ValueError(
                f"chart {request.get('chart')!r} rendered {namespace}.{kind}.{name} more than once"
            )
        kind_bucket[name] = obj

    return by_path


__all__ = ["RENDER_HELM_SPEC", "render_helm"]
