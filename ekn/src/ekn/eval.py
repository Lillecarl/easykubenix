from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import nanopynix
from nanopynix import NixError, Session


@asynccontextmanager
async def _session() -> AsyncIterator[Session]:
    async with Session(experimental_features=["flakes", "nix-command"]) as session:
        yield session


async def evaluate_file(file: Path, attr_path: str | None) -> object:
    async with (
        _session() as session,
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
        _session() as session,
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
        _session() as session,
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
        _session() as session,
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


async def evaluate_validation_config(flake_uri: str, customer: str) -> dict:
    async with (
        _session() as session,
        session.store() as store,
        session.eval(store) as eval_,
    ):
        outputs = await eval_.eval_flake(flake_uri)
        system = await (await eval_.string("builtins.currentSystem")).force_json()
        proxy = outputs.attr("eknConfig").attr(str(system)).attr(customer)

        gitops = await proxy.attr("gitops").force_json()
        generated_by_path = await proxy.attr("kubernetes").attr("generatedByPath").force_json()

        v = proxy.attr("validation")
        kubeadm_config = await v.attr("kubeadmConfig").force_json()
        pod_subnet = await v.attr("podSubnet").force_json()
        service_subnet = await v.attr("serviceSubnet").force_json()
        debug = await v.attr("debug").force_json()
        k8s_version = await proxy.attr("kubernetes").attr("package").attr("version").force_json()

        etcd_out = (await v.attr("etcdPackage").build()).get("out")
        k8s_out = (await proxy.attr("kubernetes").attr("package").build()).get("out")
        kluctl_out = (await proxy.attr("kluctl").attr("script").build()).get("out")
        manifest_out = (await proxy.attr("internal").attr("manifestJSONFile").build()).get("out")

        return {
            "config": {
                "gitops": gitops,
                "kubernetes": {
                    "generatedByPath": generated_by_path,
                    "package": {"version": k8s_version, "outPath": k8s_out},
                },
                "validation": {
                    "kubeadmConfig": kubeadm_config,
                    "podSubnet": pod_subnet,
                    "serviceSubnet": service_subnet,
                    "debug": debug,
                    "etcdPackage": {"outPath": etcd_out},
                },
                "kluctl": {"script": {"outPath": kluctl_out}},
                "internal": {"manifestJSONFile": {"outPath": manifest_out}},
            }
        }


__all__ = [
    "NixError",
    "evaluate_file",
    "evaluate_file_multi",
    "evaluate_flake",
    "evaluate_flake_ekn",
    "evaluate_validation_config",
]
