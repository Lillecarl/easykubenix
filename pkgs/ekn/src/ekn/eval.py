from __future__ import annotations

from pathlib import Path

import nanopynix
from nanopynix import NixError


async def evaluate_file(file: Path, attr_path: str | None) -> object:
    async with nanopynix.Session() as session:
        async with session.eval() as eval_:
            root = await eval_.file(str(file))

            proxy = root
            if attr_path:
                for name in attr_path.split("."):
                    if not name:
                        raise ValueError(f"empty segment in attr path: {attr_path!r}")
                    proxy = proxy.attr(name)

            return await proxy.force_json()


__all__ = [
    "NixError",
    "evaluate_file",
]
