from __future__ import annotations

import json
from pathlib import Path as SyncPath
from typing import TYPE_CHECKING

import pytest
import structlog.testing
from anyio import Path

from ekn.cli import parse
from ekn.eval import TofuUnit
from ekn.tofu import (
    TofuError,
    config_json,
    dependents,
    file_groups,
    kubeconfig_from_output,
    orphaned_state,
    prepare,
    run_chain,
    state_location,
)

if TYPE_CHECKING:
    import pathlib

#: A `tofu` that records how it was called and succeeds, so a test can assert
#: the chain without OpenTofu or a provider anywhere near it. `prepare` runs a
#: real `init` through this too.
_RECORDER = """#!/bin/sh
printf '%s %s\\n' "$(basename "$(pwd)")" "$*" >> "$TOFU_LOG"
exit 0
"""

#: Succeeds for `init` and prints a kubeconfig for `output`, so the bridge can
#: be tested without OpenTofu or a cloud behind it.
_OUTPUTS = """#!/bin/sh
printf '%s %s\\n' "$(basename "$(pwd)")" "$*" >> "$TOFU_LOG"
case "$1" in
  output) printf 'apiVersion: v1\\n' ;;
esac
exit 0
"""

#: `init` succeeds, `output` does not -- the shape a missing output has, since
#: a fake that fails at `init` never reaches the call under test.
_OUTPUT_FAILS = """#!/bin/sh
printf '%s %s\\n' "$(basename "$(pwd)")" "$*" >> "$TOFU_LOG"
case "$1" in
  output) exit 1 ;;
esac
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
    (store / "providers.json").write_text("[]\n")
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


def test_file_groups_writes_one_config_per_unit_at_its_own_path(tmp_path: pathlib.Path) -> None:
    """What `ekn commit` puts on the deploy branch, beside the manifests."""
    units = {
        "infra": _unit(tmp_path, "infra", "/nonexistent", {"resource": {}}),
        "dns": _unit(tmp_path, "dns", "/nonexistent", {"output": {}}),
    }

    assert dict(file_groups(units)) == {
        "infra/config.tf.json": '{\n  "resource": {}\n}\n',
        # The resolved provider versions travel with it: `config.tf.json` shows
        # only `required_providers` constraints, so a version bump would
        # otherwise pass through a reviewed diff invisibly.
        "infra/providers.json": "[]\n",
        "dns/config.tf.json": '{\n  "output": {}\n}\n',
        "dns/providers.json": "[]\n",
    }


def test_file_groups_refuses_two_units_at_one_path(tmp_path: pathlib.Path) -> None:
    """Two Kubernetes units may share a path -- their objects are distinct
    files. Two tf units may not: `config.tf.json` is one configuration with one
    state behind it, so the second would silently replace the first."""
    first = _unit(tmp_path, "infra", "/nonexistent", {"resource": {}})
    second = _unit(tmp_path, "dns", "/nonexistent", {"output": {}})
    second = second.model_copy(update={"target": first.target})

    with pytest.raises(TofuError, match="two tf units render to"):
        file_groups({"infra": first, "dns": second})


async def test_kubeconfig_from_output_is_private_and_temporary(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kubeconfig is a credential, so the file it lands in must not be
    readable by everyone on the machine, and must not outlive the command."""
    monkeypatch.setenv("TOFU_LOG", str(tmp_path / "calls"))
    tofu = _fake_tofu(tmp_path, "tofu", _OUTPUTS)
    unit = _unit(tmp_path, "infra", tofu, {})

    async with kubeconfig_from_output(unit, "kubeconfig", Path(tmp_path / "work")) as path:
        assert SyncPath(path).read_text() == "apiVersion: v1\n"
        assert SyncPath(path).stat().st_mode & 0o077 == 0

    assert not SyncPath(path).exists()


async def test_kubeconfig_from_output_reports_a_missing_output(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOFU_LOG", str(tmp_path / "calls"))
    unit = _unit(tmp_path, "infra", _fake_tofu(tmp_path, "tofu", _OUTPUT_FAILS), {})

    with pytest.raises(TofuError, match="tofu output -raw kubeconfig"):
        async with kubeconfig_from_output(unit, "kubeconfig", Path(tmp_path / "work")):
            pass


class TestCommandLine:
    """The `ekn tofu` surface, through the real parser rather than a double --
    so a change to a declaration is a change these see."""

    def test_each_subcommand_dispatches_to_its_own_class(self) -> None:
        assert type(parse(["tofu", "plan", "--target", "infra", "-f", "."])).__name__ == "TofuPlan"
        assert type(parse(["tofu", "apply", "--target", "infra", "-f", "."])).__name__ == "TofuApply"
        assert type(parse(["tofu", "destroy", "--target", "infra", "-f", "."])).__name__ == "TofuDestroy"

    def test_plan_never_takes_auto_approve(self) -> None:
        """`plan` changes nothing, so a confirmation flag on it would be a lie
        -- and a caller who typed it would believe an apply had been approved."""
        with pytest.raises(SystemExit):
            parse(["tofu", "plan", "--target", "infra", "--auto-approve", "-f", "."])

    def test_apply_prompts_unless_auto_approve_is_given(self) -> None:
        """Nothing sets `TF_IN_AUTOMATION`, so tofu asks as usual by default."""
        assert parse(["tofu", "apply", "--target", "infra", "-f", "."]).auto_approve is False
        assert parse(["tofu", "apply", "--target", "infra", "--auto-approve", "-f", "."]).auto_approve is True

    def test_a_target_is_required(self) -> None:
        with pytest.raises(SystemExit):
            parse(["tofu", "apply", "-f", "."])

    def test_kubeapply_takes_a_tofu_kubeconfig(self) -> None:
        command = parse(["kubeapply", "--kubeconfig-from-tofu", "infra:kubeconfig", "-f", "."])
        assert command.kubeconfig_from_tofu == "infra:kubeconfig"
        assert parse(["kubeapply", "-f", "."]).kubeconfig_from_tofu is None


class TestDependents:
    """The reverse of what `apply` walks, and the direction `destroy` needs."""

    @staticmethod
    def _chain(tmp_path: pathlib.Path) -> list[TofuUnit]:
        """network <- cluster <- ingress, each closure already transitive as
        easykubenix resolves it."""
        units = [_unit(tmp_path, name, "/nonexistent", {}) for name in ("network", "cluster", "ingress")]
        return [
            units[0],
            units[1].model_copy(update={"dependencies": ["network"]}),
            units[2].model_copy(update={"dependencies": ["network", "cluster"]}),
        ]

    def test_a_leaf_has_no_dependents(self, tmp_path: pathlib.Path) -> None:
        assert dependents(self._chain(tmp_path), "ingress") == []

    def test_a_base_unit_names_everything_that_needs_it(self, tmp_path: pathlib.Path) -> None:
        """Destroying `network` would leave both of these pointing at nothing,
        which is what the refusal exists to prevent."""
        assert dependents(self._chain(tmp_path), "network") == ["cluster", "ingress"]

    def test_a_middle_unit_names_only_what_is_above_it(self, tmp_path: pathlib.Path) -> None:
        assert dependents(self._chain(tmp_path), "cluster") == ["ingress"]


async def test_prepare_skips_init_when_nothing_changed(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--kubeconfig-from-tofu` calls `prepare` only to read one output, and
    re-initialising there is a provider download and a backend round trip for a
    question already answered."""
    monkeypatch.setenv("TOFU_LOG", str(tmp_path / "calls"))
    unit = _unit(tmp_path, "infra", _fake_tofu(tmp_path, "tofu", _RECORDER), {})
    root = Path(tmp_path / "work")

    workdir = await prepare(unit, root)
    # The fake writes no `.terraform/`; a real `tofu init` does.
    await (workdir / ".terraform").mkdir()
    await prepare(unit, root)

    assert (tmp_path / "calls").read_text() == "infra init -input=false\n"


async def test_prepare_re_inits_when_the_config_changed(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `.terraform/` alone is not enough -- the configuration is what decides
    whether the initialised directory still matches."""
    monkeypatch.setenv("TOFU_LOG", str(tmp_path / "calls"))
    unit = _unit(tmp_path, "infra", _fake_tofu(tmp_path, "tofu", _RECORDER), {})
    root = Path(tmp_path / "work")

    workdir = await prepare(unit, root)
    await (workdir / ".terraform").mkdir()
    await (workdir / "config.tf.json").write_text('{"resource": {}}')
    await prepare(unit, root)

    assert (tmp_path / "calls").read_text().count("init") == 2


class TestStateLocation:
    """Reported on every run, because easykubenix asserts nothing about a
    backend and a unit that forgot one gets silent local state."""

    async def test_a_missing_backend_says_local_and_not_committed(self, tmp_path: pathlib.Path) -> None:
        workdir = Path(tmp_path)
        await (workdir / "config.tf.json").write_text("{}")

        assert await state_location(workdir) == f"{workdir}/terraform.tfstate (local, not committed)"

    async def test_an_explicit_local_backend_says_the_same(self, tmp_path: pathlib.Path) -> None:
        workdir = Path(tmp_path)
        await (workdir / "config.tf.json").write_text('{"terraform": {"backend": {"local": {}}}}')

        assert "local, not committed" in await state_location(workdir)

    async def test_a_remote_backend_is_named(self, tmp_path: pathlib.Path) -> None:
        workdir = Path(tmp_path)
        await (workdir / "config.tf.json").write_text('{"terraform": {"backend": {"s3": {"bucket": "b"}}}}')

        assert await state_location(workdir) == "s3 backend"


class TestOrphanedState:
    """A `tf` unit deleted from the Nix config leaves its infrastructure
    running with nothing able to name it. The Kubernetes half of the same
    refactor is safe, because prune selects by label."""

    async def test_nothing_is_orphaned_when_every_directory_has_a_unit(self, tmp_path: pathlib.Path) -> None:
        root = Path(tmp_path / "work")
        await (root / "infra").mkdir(parents=True)
        units = [_unit(tmp_path, "infra", "/nonexistent", {})]

        assert await orphaned_state(units, root) == (1, [])

    async def test_a_deleted_unit_leaves_its_state_behind(self, tmp_path: pathlib.Path) -> None:
        root = Path(tmp_path / "work")
        await (root / "infra").mkdir(parents=True)
        await (root / "dns").mkdir()

        assert await orphaned_state([_unit(tmp_path, "infra", "/nonexistent", {})], root) == (2, ["dns"])

    async def test_no_root_yet_is_not_an_orphan(self, tmp_path: pathlib.Path) -> None:
        """Nothing has run here, so there is nothing to have leaked."""
        assert await orphaned_state([], Path(tmp_path / "never-created")) == (0, [])


class TestAdoptionNotice:
    """The one sharp edge in migrating an existing tree: the state copy has to
    happen before the first run, and missing it plans to create what already
    exists."""

    @staticmethod
    async def _lines(tmp_path: pathlib.Path, *, with_state: bool) -> list[str]:
        unit = _unit(tmp_path, "infra", _fake_tofu(tmp_path, "tofu", _RECORDER), {})
        root = Path(tmp_path / "work")
        if with_state:
            await (root / "infra").mkdir(parents=True)
            await (root / "infra" / "terraform.tfstate").write_text("{}")
        messages: list[str] = []
        with structlog.testing.capture_logs() as logs:
            await prepare(unit, root)
            messages.extend(str(entry.get("event", "")) for entry in logs)
        return messages

    async def test_says_so_when_there_is_no_state_to_adopt(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TOFU_LOG", str(tmp_path / "calls"))

        assert any("no existing state" in line for line in await self._lines(tmp_path, with_state=False))

    async def test_stays_quiet_once_state_exists(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Self-limiting: after the first apply the file is there and the
        notice stops, so a steady-state run never carries it."""
        monkeypatch.setenv("TOFU_LOG", str(tmp_path / "calls"))

        assert not any("no existing state" in line for line in await self._lines(tmp_path, with_state=True))
