from __future__ import annotations

from pathlib import Path

import nanopynix
from nanopynix import NixError


_SESSION_KWARGS = {
    "experimental_features": ["flakes", "nix-command"],
}


async def evaluate_file(file: Path, attr_path: str | None) -> object:
    async with (
        nanopynix.Session(**_SESSION_KWARGS) as session,
        session.store() as store,
        session.eval(store) as eval_,
    ):
        root = await eval_.file(str(file))

        proxy = root
        if attr_path:
            for name in attr_path.split("."):
                if not name:
                    raise ValueError(f"empty segment in attr path: {attr_path!r}")
                proxy = proxy.attr(name)

        return await proxy.force_json()


async def evaluate_file_multi(
    file: Path,
    *attr_paths: str | None,
) -> list[object]:
    results: list[object] = []
    async with (
        nanopynix.Session(**_SESSION_KWARGS) as session,
        session.store() as store,
        session.eval(store) as eval_,
    ):
        root = await eval_.file(str(file))
        for attr_path in attr_paths:
            proxy = root
            if attr_path:
                for name in attr_path.split("."):
                    if not name:
                        raise ValueError(
                            f"empty segment in attr path: {attr_path!r}"
                        )
                    proxy = proxy.attr(name)
            results.append(await proxy.force_json())
    return results


async def evaluate_flake(flake_uri: str, attr_path: str | None) -> object:
    async with (
        nanopynix.Session(**_SESSION_KWARGS) as session,
        session.store() as store,
        session.eval(store) as eval_,
    ):
        root = await eval_.eval_flake(flake_uri)

        proxy = root
        if attr_path:
            for name in attr_path.split("."):
                if not name:
                    raise ValueError(f"empty segment in attr path: {attr_path!r}")
                proxy = proxy.attr(name)

        return await proxy.force_json()


async def evaluate_flake_ekn(flake_uri: str, customer: str) -> dict:
    async with (
        nanopynix.Session(**_SESSION_KWARGS) as session,
        session.store() as store,
        session.eval(store) as eval_,
    ):
        outputs = await eval_.eval_flake(flake_uri)
        system = await (await eval_.string("builtins.currentSystem")).force_json()
        proxy = outputs.attr("eknConfig").attr(str(system)).attr(customer)

        gitops = await proxy.attr("gitops").force_json()
        generated_by_path = await proxy.attr("kubernetes").attr("generatedByPath").force_json()
        return {
            "config": {
                "gitops": gitops,
                "kubernetes": {"generatedByPath": generated_by_path},
            }
        }


__all__ = [
    "NixError",
    "evaluate_file",
    "evaluate_file_multi",
    "evaluate_flake",
    "evaluate_flake_ekn",
]
