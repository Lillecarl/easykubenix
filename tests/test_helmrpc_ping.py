from __future__ import annotations

import shutil

from grpclib_transports import stdio_worker
from helmrpc_proto.helmrpc_grpc import HelmStub
from helmrpc_proto.helmrpc_pb2 import PingRequest


async def test_helmrpc_ping_roundtrips_over_stdio() -> None:
    """Drive the helmrpc Go binary from Python over a stdio-transported gRPC channel."""
    helmrpc = shutil.which("helmrpc")
    assert helmrpc is not None, "helmrpc must be on PATH (see nix/shell.nix)"

    async with stdio_worker([helmrpc]) as channel:
        stub = HelmStub(channel)
        response = await stub.Ping(PingRequest(message="hello"))

    assert response.message == "hello"
