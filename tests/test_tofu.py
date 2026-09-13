from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from anyio import Path

from ekn.eval import TofuUnit
from ekn.tofu import TofuError, config_json, prepare, run_chain

if TYPE_CHECKING:
    import pathlib

#: A `tofu` that records how it was called and succeeds, so a test can assert
#: the chain without OpenTofu or a provider anywhere near it. `prepare` runs a
#: real `init` through this too.
_RECORDER = """#!/bin/sh
printf '%s %s\\n' "$(basename "$(pwd)")" "$*" >> "$TOFU_LOG"
exit 0
"""

_FAILING = """#!/bin/sh
printf '%s %s\\n' "$(basename "$(pwd)")" "$*" >> "$TOFU_LOG"
exit 1
"""


def _fake_tofu(tmp_path: pathlib.Path, name: str, script: str) -> str:
    path = tmp_path / name
    path.write_text(script)
    path.chmod(0o755)
    return str(path)


def _unit(tmp_path: pathlib.Path, name: str, tofu: str, config: dict[str, object]) -> TofuUnit:
    """A `TofuUnit` over a real directory, standing in for a store path."""
    store = tmp_path / f"store-{name}"
    store.mkdir()
    (store / "config.tf.json").write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
    (store / "config.tf.json").chmod(0o444)
    return TofuUnit.model_validate(
        {
            "name": name,
            "configFile": str(store),
            "tofu": tofu,
            "dependencies": [],
            "target": {"path": name},
        }
    )


async def test_prepare_copies_the_config_out_of_the_read_only_store(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOFU_LOG", str(tmp_path / "calls"))
    unit = _unit(tmp_path, "infra", _fake_tofu(tmp_path, "tofu", _RECORDER), {"resource": {}})

    workdir = await prepare(unit, Path(tmp_path / "work"))

    # Writable, which the store copy is not -- `tofu init` writes `.terraform/`
    # beside the configuration and cannot run in the store at all.
    assert await (workdir / "config.tf.json").exists()
    assert json.loads(await (workdir / "config.tf.json").read_text()) == {"resource": {}}
    assert (tmp_path / "calls").read_text() == "infra init -input=false\n"


async def test_prepare_is_repeatable_and_refreshes_the_config(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second run must not fail on the read-only copy left by the first, and
    must overwrite a hand-edit rather than keep it -- the store is the source."""
    monkeypatch.setenv("TOFU_LOG", str(tmp_path / "calls"))
    unit = _unit(tmp_path, "infra", _fake_tofu(tmp_path, "tofu", _RECORDER), {"resource": {}})
    root = Path(tmp_path / "work")

    workdir = await prepare(unit, root)
    await (workdir / "config.tf.json").write_text('{"edited": true}')
    await prepare(unit, root)

    assert json.loads(await (workdir / "config.tf.json").read_text()) == {"resource": {}}


async def test_prepare_removes_a_stale_lock_file(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`.terraform.lock.hcl` pins a provider version that the next nixpkgs pin
    may not supply, and `init` then fails against a lock nobody wrote."""
    monkeypatch.setenv("TOFU_LOG", str(tmp_path / "calls"))
    unit = _unit(tmp_path, "infra", _fake_tofu(tmp_path, "tofu", _RECORDER), {})
    root = Path(tmp_path / "work")

    workdir = await prepare(unit, root)
    await (workdir / ".terraform.lock.hcl").write_text("provider ...")
    await prepare(unit, root)

    assert not await (workdir / ".terraform.lock.hcl").exists()


async def test_run_chain_runs_every_unit_in_order(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A dependency closure is separate invocations, deepest first -- not one
    merged plan, because OpenTofu has no `ekn.resourcePriority` equivalent."""
    monkeypatch.setenv("TOFU_LOG", str(tmp_path / "calls"))
    tofu = _fake_tofu(tmp_path, "tofu", _RECORDER)
    units = [_unit(tmp_path, name, tofu, {}) for name in ("network", "cluster", "apps")]

    await run_chain(units, ["apply", "-auto-approve"], Path(tmp_path / "work"))

    assert (tmp_path / "calls").read_text().splitlines() == [
        "network init -input=false",
        "network apply -auto-approve",
        "cluster init -input=false",
        "cluster apply -auto-approve",
        "apps init -input=false",
        "apps apply -auto-approve",
    ]


async def test_run_chain_stops_at_the_first_failure(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The units after a failure are the ones that needed it, so running them
    against a half-built dependency is worse than not running them."""
    monkeypatch.setenv("TOFU_LOG", str(tmp_path / "calls"))
    good = _fake_tofu(tmp_path, "tofu-ok", _RECORDER)
    bad = _fake_tofu(tmp_path, "tofu-bad", _FAILING)
    units = [
        _unit(tmp_path, "network", good, {}),
        _unit(tmp_path, "cluster", bad, {}),
        _unit(tmp_path, "apps", good, {}),
    ]

    with pytest.raises(TofuError, match="cluster"):
        await run_chain(units, ["apply"], Path(tmp_path / "work"))

    assert "apps" not in (tmp_path / "calls").read_text()


def test_config_json_is_the_stores_own_bytes(tmp_path: pathlib.Path) -> None:
    """`ekn commit` writes what Nix rendered, not a re-serialisation of it --
    `tofu.nix` pretty-prints through `jq --sort-keys` so the file reads as a
    diff, and round-tripping through Python would throw that away."""
    unit = _unit(tmp_path, "infra", "/nonexistent", {"terraform": {"required_version": ">= 1.6"}})

    assert config_json(unit) == '{\n  "terraform": {\n    "required_version": ">= 1.6"\n  }\n}\n'
