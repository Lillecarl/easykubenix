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
# attrs/list/scalar), so the request attrset and the rendered resource list
# cross as real Nix values -- no builtins.toJSON/builtins.fromJSON needed on
# either side. Field names mirror easykubenix/pkgs/chart2json.nix's (chart,
# name, namespace, values, kubeVersion, includeCRDs, noHooks, apiVersions).
RENDER_HELM_SPEC = PrimOpSpec(
    name="renderHelm",
    arity=1,
    args=["request"],
    doc=(
        "Render a Helm chart with `helm template` semantics via the helmrpc "
        "gRPC service. Takes an attrset with fields chart, name, namespace, "
        "values, kubeVersion, includeCRDs, noHooks, apiVersions and returns "
        "the rendered resources as a list of attrsets."
    ),
    rpc=True,
)


async def render_helm(request: dict[str, Any]) -> list[dict[str, Any]]:
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

    return [MessageToDict(resource) for resource in response.resources]


__all__ = ["RENDER_HELM_SPEC", "render_helm"]
