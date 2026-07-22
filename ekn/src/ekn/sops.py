from __future__ import annotations

import asyncio
import json
from typing import Any


class SopsDecryptError(RuntimeError):
    pass


async def maybe_decrypt(obj: dict[str, Any]) -> dict[str, Any]:
    """Decrypt `obj` via the real `sops` CLI if it carries a `sops:`
    metadata block (the standard marker SOPS itself writes) -- otherwise
    return it unchanged.

    Shells out to `sops` rather than reimplementing its crypto/file format:
    ekn never encrypts anything itself, and SOPS-encrypted objects flow
    through the rest of the pipeline (`kubernetes.generated`, `ekn commit`'s
    git tree) untouched. This is the one place a direct apply (`ekn
    kubeapply`/`ekn validate` -- both bypass ArgoCD+kustomize+ksops
    entirely) needs to actually decrypt before the apiserver can use it.
    """
    if not isinstance(obj.get("sops"), dict):
        return obj

    proc = await asyncio.create_subprocess_exec(
        "sops",
        "--decrypt",
        "--input-type",
        "json",
        "--output-type",
        "json",
        "/dev/stdin",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(json.dumps(obj).encode())
    if proc.returncode != 0:
        kind = obj.get("kind", "?")
        name = obj.get("metadata", {}).get("name", "?")
        raise SopsDecryptError(
            f"sops --decrypt failed for {kind}/{name}: {stderr.decode()}"
        )
    decrypted = json.loads(stdout)
    if not isinstance(decrypted, dict):
        raise SopsDecryptError(f"sops --decrypt returned non-object JSON: {stdout!r}")
    return decrypted


__all__ = ["SopsDecryptError", "maybe_decrypt"]
