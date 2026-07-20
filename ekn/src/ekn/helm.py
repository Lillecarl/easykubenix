from __future__ import annotations

import shutil
from typing import Any

from grpclib_transports import stdio_worker
from helmrpc_proto.helmrpc_grpc import HelmStub
from helmrpc_proto.helmrpc_pb2 import RenderRequest
from nanopynix import PrimOpSpec
from pydantic_core import from_json, to_json

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

    # RenderRequest/RenderResponse each wrap a single opaque JSON payload
    # rather than typed protobuf fields -- see helmrpc.proto for why (in
    # short: google.protobuf.Struct's per-nesting-level submessages blow past
    # upb's hardcoded ~100-message decode depth on deep CRD OpenAPI schemas).
    # pydantic_core is already a real dependency (nanopynix-proto's
    # betterproto2 codegen uses pydantic dataclasses) and its from_json/
    # to_json are Rust-backed, so there's no reason to reach for stdlib json
    # here instead.
    request_body = {
        "chart": request["chart"],
        "name": request.get("name", ""),
        "namespace": request.get("namespace", ""),
        "values": request.get("values") or {},
        "kubeVersion": request.get("kubeVersion", ""),
        "includeCRDs": request.get("includeCRDs", False),
        "noHooks": request.get("noHooks", False),
        "apiVersions": request.get("apiVersions", []),
    }

    async with stdio_worker([helmrpc]) as channel:
        stub = HelmStub(channel)
        response = await stub.Render(RenderRequest(request_json=to_json(request_body)))

    response_body = from_json(response.response_json)

    by_path: dict[str, Any] = {}
    for obj in response_body["resources"]:
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
