"""`ekn.preApplyCommand` runs before `ekn kubeapply` touches the cluster.

The pre half of easykubenix issue #5. It exists for a store path
`ekn.cacheTo` cannot place: a cache the cluster reads but this machine
cannot write through the push, or one that must hold a path before the
workload serving `ekn.cacheTo` restarts.

A real script on disk rather than a mock, because the contract is what the
program sees -- argv, the environment, and what stops the apply.
"""

from __future__ import annotations

import json
import os
import pathlib
import uuid

import anyio
import pytest

from ekn.cli import _run_pre_apply
from ekn.eval import KubeApplyConfigResult, evaluate_kubeapply_config

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Records argv and the three variables `ekn` promises, then succeeds.
_RECORDER = """#!/bin/sh
{
  printf 'argv1=%s\\n' "$1"
  printf 'manifest=%s\\n' "${EKN_MANIFEST-unset}"
  printf 'environment=%s\\n' "${EKN_ENVIRONMENT-unset}"
  printf 'target=%s\\n' "${EKN_TARGET-unset}"
  printf 'cache_push=%s\\n' "${EKN_CACHE_PUSH-unset}"
  printf 'allow_failure=%s\\n' "${EKN_CACHE_ALLOW_FAILURE-unset}"
  printf 'paths=%s\\n' "$(grep -o '/nix/store/[a-z0-9]\\{32\\}-[^"]*' "$1" | sort -u | tr '\\n' ',')"
} >> "$HOOK_LOG"
exit 0
"""

_FAILING = """#!/bin/sh
echo "the cache push failed" >&2
exit 3
"""


def _hook(tmp_path: pathlib.Path, script: str) -> str:
    path = tmp_path / "pre-apply"
    path.write_text(script)
    path.chmod(0o755)
    return str(path)


def _config(command: str | None, objects: list[dict[str, object]] | None = None) -> KubeApplyConfigResult:
    return KubeApplyConfigResult.model_validate(
        {
            "groups": [
                {
                    "unit": None,
                    "field_manager": "ekn",
                    "objects": objects
                    if objects is not None
                    else [{"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "c"}}],
                }
            ],
            "environment": "nixlab2",
            "resource_priority": {},
            "sops_age_identities": [],
            "preApplyCommand": command,
        }
    )


class TestTheContract:
    async def test_the_hook_sees_the_manifest_and_the_environment(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log = tmp_path / "hook-log"
        monkeypatch.setenv("HOOK_LOG", str(log))

        await _run_pre_apply(_config(_hook(tmp_path, _RECORDER)), target="argocd")

        seen = dict(line.split("=", 1) for line in log.read_text().splitlines())
        assert seen["argv1"] == seen["manifest"], "the first argument and EKN_MANIFEST are one path"
        assert seen["environment"] == "nixlab2"
        assert seen["target"] == "argocd"

    async def test_a_whole_instance_apply_has_an_empty_target(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty, not unset. A hook that branches on the variable can then
        read it without having to know which shape `ekn` chose."""
        log = tmp_path / "hook-log"
        monkeypatch.setenv("HOOK_LOG", str(log))

        await _run_pre_apply(_config(_hook(tmp_path, _RECORDER)), target=None)

        seen = dict(line.split("=", 1) for line in log.read_text().splitlines())
        assert seen["target"] == ""

    async def test_the_store_paths_are_readable_out_of_the_manifest(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole reason a seeding hook needs no extra option: every path
        the apply names is in the manifest as text, context or no context."""
        log = tmp_path / "hook-log"
        monkeypatch.setenv("HOOK_LOG", str(log))
        env_path = "/nix/store/" + "a" * 32 + "-cacheEnv"
        objects = [
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "c"},
                "data": {"env": env_path},
            }
        ]

        await _run_pre_apply(_config(_hook(tmp_path, _RECORDER), objects), target=None)

        seen = dict(line.split("=", 1) for line in log.read_text().splitlines())
        assert env_path in seen["paths"]

    async def test_the_manifest_is_gone_afterwards(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log = tmp_path / "hook-log"
        monkeypatch.setenv("HOOK_LOG", str(log))

        await _run_pre_apply(_config(_hook(tmp_path, _RECORDER)), target=None)

        seen = dict(line.split("=", 1) for line in log.read_text().splitlines())
        assert not await anyio.Path(seen["manifest"]).exists()


class TestWhatTheDeployTolerates:
    """`ekn` never softens the abort. It tells the hook what the deploy
    around it tolerates and lets the script decide for itself."""

    async def test_the_flags_reach_the_hook(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        log = tmp_path / "hook-log"
        monkeypatch.setenv("HOOK_LOG", str(log))

        await _run_pre_apply(
            _config(_hook(tmp_path, _RECORDER)),
            target=None,
            cache_push=False,
            cache_allow_failure=True,
        )

        seen = dict(line.split("=", 1) for line in log.read_text().splitlines())
        # `1`/`0`, so `[ "$X" = 1 ]` in a POSIX shell needs no case handling.
        assert seen["cache_push"] == "0"
        assert seen["allow_failure"] == "1"

    async def test_the_ordinary_deploy_says_so_too(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Always set, never absent. A hook that reads them can use `$X` with
        no default rather than guessing which shape it got."""
        log = tmp_path / "hook-log"
        monkeypatch.setenv("HOOK_LOG", str(log))

        await _run_pre_apply(_config(_hook(tmp_path, _RECORDER)), target=None)

        seen = dict(line.split("=", 1) for line in log.read_text().splitlines())
        assert seen["cache_push"] == "1"
        assert seen["allow_failure"] == "0"


class TestWhatStopsTheApply:
    async def test_a_failing_hook_aborts(self, tmp_path: pathlib.Path) -> None:
        """Issue #5's point 3. A hook that pushes a closure and fails,
        ignored, leaves a cluster pointed at paths nothing it can reach
        holds -- and the apply that follows would look like it worked."""
        with pytest.raises(SystemExit) as exc:
            await _run_pre_apply(_config(_hook(tmp_path, _FAILING)), target=None)

        assert exc.value.code == 1

    async def test_cache_allow_failure_does_not_soften_it(self, tmp_path: pathlib.Path) -> None:
        """That flag decides what to do about a cache push `ekn` runs. A hook
        that exits non-zero has made its own decision, and `ekn` obeys it --
        a hook that wants to be advisory exits 0."""
        with pytest.raises(SystemExit) as exc:
            await _run_pre_apply(
                _config(_hook(tmp_path, _FAILING)),
                target=None,
                cache_allow_failure=True,
            )

        assert exc.value.code == 1

    async def test_no_command_runs_nothing(self) -> None:
        await _run_pre_apply(_config(None), target=None)


class TestTheManifestIsTheSlice:
    async def test_a_target_apply_writes_only_that_slice(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`cfg.objects` is what this apply sends. A hook told about the
        whole instance would seed paths the apply does not name, and miss a
        seed rewriting one it does."""
        monkeypatch.setenv("HOOK_LOG", str(tmp_path / "hook-log"))
        captured = tmp_path / "copy.json"
        script = f"""#!/bin/sh
cp "$1" {captured}
exit 0
"""
        objects = [
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "only-me"}},
        ]

        await _run_pre_apply(_config(_hook(tmp_path, script), objects), target="one-unit")

        written = json.loads(captured.read_text())
        assert written["kind"] == "List"
        assert [item["metadata"]["name"] for item in written["items"]] == ["only-me"]


class TestTheOptionReachesPython:
    """A real instance, so the option's type and its place in the tree are
    part of what this covers. `types.path`, so a derivation is accepted and
    its Nix string context is kept -- `ekn` realises the program before it
    runs it, which a `str` option would not."""

    @staticmethod
    def _instance(tmp_path: pathlib.Path, ekn_lines: str) -> pathlib.Path:
        f = tmp_path / "instance.nix"
        f.write_text(f"""
            let
              sources = import {PROJECT_ROOT}/nix/sources.nix;
              pkgs = import sources.nixpkgs {{ }};
            in
            import {PROJECT_ROOT} {{
              inherit pkgs;
              modules = [
                ({{ pkgs, ... }}: {{
                  ekn.environment = "easykubenix";
                  {ekn_lines}
                  kubernetes.objects.default.ConfigMap.c.data.key = "value";
                }})
              ];
            }}
        """)
        return f

    async def test_a_derivation_is_realised_and_executable(self, tmp_path: pathlib.Path) -> None:
        # A fresh store path every run, so "already built by something else"
        # cannot make this pass. Evaluation computes the path; only a build
        # puts it on disk, and that is the difference being asserted.
        nonce = uuid.uuid4().hex
        f = self._instance(
            tmp_path,
            f'ekn.preApplyCommand = pkgs.writeShellScript "seed-{nonce}" "exit 0";',
        )

        cfg = await evaluate_kubeapply_config(f, None, None, None, None)

        assert cfg.pre_apply_command is not None
        assert cfg.pre_apply_command.startswith("/nix/store/")
        # Realised, not merely named. A path Nix computed but never built is
        # a hook that fails with ENOENT at the worst possible moment.
        assert os.access(cfg.pre_apply_command, os.X_OK), "the program is on disk and executable"

    async def test_no_hook_is_the_default(self, tmp_path: pathlib.Path) -> None:
        cfg = await evaluate_kubeapply_config(self._instance(tmp_path, ""), None, None, None, None)
        assert cfg.pre_apply_command is None
