from __future__ import annotations

import json
import shutil

from google.protobuf.json_format import MessageToDict
from google.protobuf.struct_pb2 import Struct
from grpclib_transports import stdio_worker
from helmrpc_proto.helmrpc_grpc import HelmStub
from helmrpc_proto.helmrpc_pb2 import RenderRequest
from nanopynix import PrimOpSpec

# RPC primop args/return values are limited to Nix scalars (str/int/float/
# bool/null), so the whole request and the resulting resource list are
# carried as JSON strings -- Nix-side callers wrap with builtins.toJSON /
# builtins.fromJSON, mirroring easykubenix/pkgs/chart2json.nix's field names
# (chart, name, namespace, values, kubeVersion, includeCRDs, noHooks,
# apiVersions).
RENDER_HELM_SPEC = PrimOpSpec(
    name="renderHelm",
    arity=1,
    args=["requestJSON"],
    doc=(
        "Render a Helm chart with `helm template` semantics via the helmrpc "
        "gRPC service. Takes a JSON string with fields chart, name, "
        "namespace, values, kubeVersion, includeCRDs, noHooks, apiVersions "
        "and returns a JSON string containing the rendered resources as a "
        "list -- decode both sides with builtins.toJSON/builtins.fromJSON."
    ),
    rpc=True,
)


async def render_helm(request_json: str) -> str:
    request = json.loads(request_json)

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

    resources = [MessageToDict(resource) for resource in response.resources]
    return json.dumps(resources)


__all__ = ["RENDER_HELM_SPEC", "render_helm"]
