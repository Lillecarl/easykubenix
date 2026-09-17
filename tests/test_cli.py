from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, ClassVar

import pytest

from ekn.cli import Deploy, JsonToYaml, Validate, YamlToJson, _apply_groups, _push_ekn_cache
from ekn.eval import (
    CacheConfigResult,
    KubeApplyConfigResult,
    evaluate_cache_config,
    evaluate_file,
    evaluate_flake_ekn,
)
from ekn.git import commit_manifests, diff_manifests
from ekn.gitops import flatten_manifests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CUSTOMER_NIX = """\
{
  customer1 = {
    config = {
      gitops = { enable = true; branch = "test-render"; };
      kubernetes = {
        generated = [
          {
            apiVersion = "v1"; kind = "ConfigMap";
            metadata = { name = "my-config"; namespace = "default"; };
            data = { key = "value"; };
          }
        ];
      };
    };
  };
}
"""


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    import subprocess

    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "--allow-empty", "-q", "-m", "root"], capture_output=True, check=True
    )
    return repo


class TestEval:
    async def test_eval_callback(self, tmp_path: Path) -> None:
        f = tmp_path / "customers.nix"
        f.write_text(CUSTOMER_NIX)
        result = await evaluate_file(f, None)
        assert isinstance(result, dict)
        assert "customer1" in result

    async def test_eval_with_attr(self, tmp_path: Path) -> None:
        f = tmp_path / "customers.nix"
        f.write_text(CUSTOMER_NIX)
        result = await evaluate_file(f, "customer1")
        assert isinstance(result, dict)
        assert "config" in result


class TestDiff:
    # There is no instance-wide GitOps path any more -- a path belongs to a
    # target (`deployment.units.<name>.path`, see tests/test_gitops.py) and the
    # root default now lives in `flatten_manifests`' `subdir` argument.

    async def test_diff_no_changes(self, tmp_path: Path, git_repo: Path) -> None:
        f = tmp_path / "customers.nix"
        f.write_text(CUSTOMER_NIX)
        os.environ["EKN_REPO"] = str(git_repo)
        try:
            result = await evaluate_file(f, "customer1")
            files = flatten_manifests(result["config"]["kubernetes"]["generated"])
            commit_manifests(".", result["config"]["gitops"]["branch"], files, "seed")
            diff_out = diff_manifests(".", result["config"]["gitops"]["branch"], files)
            assert diff_out is None
        finally:
            os.environ.pop("EKN_REPO", None)

    async def test_diff_without_gitops_errors(self, tmp_path: Path) -> None:
        f = tmp_path / "no.nix"
        f.write_text("""\
{ app = {
    config = {
      gitops = { enable = false; };
      kubernetes = { generated = []; };
    };
  };
}
""")
        result = await evaluate_file(f, "app")
        assert result["config"]["gitops"]["enable"] is False

    async def test_diff_branch_override(self, tmp_path: Path, git_repo: Path) -> None:
        f = tmp_path / "customers.nix"
        f.write_text(CUSTOMER_NIX)
        os.environ["EKN_REPO"] = str(git_repo)
        try:
            result = await evaluate_file(f, "customer1")
            default_files = flatten_manifests(
                [
                    {
                        "apiVersion": "v1",
                        "kind": "ConfigMap",
                        "metadata": {"name": "my-config", "namespace": "default"},
                        "data": {"key": "original"},
                    }
                ]
            )
            commit_manifests(".", "override-branch", default_files, "first")
            new_files = flatten_manifests(result["config"]["kubernetes"]["generated"])
            diff_out = diff_manifests(".", "override-branch", new_files)
            assert diff_out is not None
            assert "my-config" in diff_out or "test" in diff_out
        finally:
            os.environ.pop("EKN_REPO", None)

    def test_diff_ignores_source_worktree_files(self, git_repo: Path) -> None:
        source_file = git_repo / "flake.nix"
        source_file.write_text("{ outputs = _: {}; }\n")
        subprocess.run(["git", "-C", str(git_repo), "add", "flake.nix"], check=True)

        os.environ["EKN_REPO"] = str(git_repo)
        try:
            diff_out = diff_manifests(
                ".",
                "rendered",
                [("clusters/demo/none/Namespace/demo.yaml", "kind: Namespace\n")],
            )
        finally:
            os.environ.pop("EKN_REPO", None)

        assert diff_out is not None
        assert "clusters/demo/none/Namespace/demo.yaml" in diff_out
        assert "flake.nix" not in diff_out


class _FakeStdin:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self.buffer = io.BytesIO(data)

    def read(self) -> str:
        return self._data.decode()


class _FakeStdout:
    def __init__(self) -> None:
        self.buffer = io.BytesIO()
        self._text_parts: list[str] = []

    def write(self, text: str) -> None:
        self._text_parts.append(text)

    @property
    def text(self) -> str:
        return "".join(self._text_parts)


class TestYamlJsonConversion:
    """`ekn _yamlToJson`/`ekn _jsonToYAML` -- the hidden CLI subcommands
    importyaml.nix's derivation fallback shells out to instead of `yq`, so
    that path shares nanopynix's YAML-parsing code (and its yaml11/yaml12
    scalar-resolution differences) instead of yq's."""

    async def test_yaml_to_json_defaults_to_yaml12_decimal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stdout = _FakeStdout()
        monkeypatch.setattr(sys, "stdin", _FakeStdin(b"mode: 0644\n"))
        monkeypatch.setattr(sys, "stdout", stdout)
        cmd = object.__new__(YamlToJson)
        cmd.yaml_version = "yaml12"

        await YamlToJson.run(cmd)

        assert json.loads(stdout.buffer.getvalue()) == [{"mode": 644}]

    async def test_yaml_to_json_yaml11_resolves_octal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stdout = _FakeStdout()
        monkeypatch.setattr(sys, "stdin", _FakeStdin(b"mode: 0644\n"))
        monkeypatch.setattr(sys, "stdout", stdout)
        cmd = object.__new__(YamlToJson)
        cmd.yaml_version = "yaml11"

        await YamlToJson.run(cmd)

        assert json.loads(stdout.buffer.getvalue()) == [{"mode": 420}]

    async def test_yaml_to_json_multi_document_stream(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stdout = _FakeStdout()
        monkeypatch.setattr(sys, "stdin", _FakeStdin(b"a: 1\n---\nb: 2\n"))
        monkeypatch.setattr(sys, "stdout", stdout)
        cmd = object.__new__(YamlToJson)
        cmd.yaml_version = "yaml12"

        await YamlToJson.run(cmd)

        assert json.loads(stdout.buffer.getvalue()) == [{"a": 1}, {"b": 2}]

    async def test_yaml_to_json_preserves_empty_documents_as_null(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # `_yamlToJson` dumps the raw parsed document stream -- dropping
        # empty/null documents (e.g. a leading `---` in a K8s manifest
        # bundle) is importyaml.nix's job, applied uniformly to both the
        # in-process primop path and this CLI fallback, same as
        # renderChart.nix's `rendered = lib.filter (object: object != null)`.
        stdout = _FakeStdout()
        monkeypatch.setattr(sys, "stdin", _FakeStdin(b"---\n---\na: 1\n"))
        monkeypatch.setattr(sys, "stdout", stdout)
        cmd = object.__new__(YamlToJson)
        cmd.yaml_version = "yaml12"

        await YamlToJson.run(cmd)

        assert json.loads(stdout.buffer.getvalue()) == [None, {"a": 1}]

    async def test_json_to_yaml_renders_a_document_stream(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stdout = _FakeStdout()
        payload = json.dumps([{"kind": "ConfigMap", "metadata": {"name": "foo"}}]).encode()
        monkeypatch.setattr(sys, "stdin", _FakeStdin(payload))
        monkeypatch.setattr(sys, "stdout", stdout)
        cmd = object.__new__(JsonToYaml)

        await JsonToYaml.run(cmd)

        assert stdout.text.startswith("---\n")
        assert "kind: ConfigMap" in stdout.text
        assert "name: foo" in stdout.text

    async def test_yaml_to_json_to_yaml_round_trips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        source = b"kind: ConfigMap\nmetadata:\n  name: foo\ndata:\n  mode: 0644\n"
        to_json_stdout = _FakeStdout()
        monkeypatch.setattr(sys, "stdin", _FakeStdin(source))
        monkeypatch.setattr(sys, "stdout", to_json_stdout)
        to_json = object.__new__(YamlToJson)
        to_json.yaml_version = "yaml11"
        await YamlToJson.run(to_json)
        [parsed] = json.loads(to_json_stdout.buffer.getvalue())
        assert parsed == {"kind": "ConfigMap", "metadata": {"name": "foo"}, "data": {"mode": 420}}

        to_yaml_stdout = _FakeStdout()
        monkeypatch.setattr(sys, "stdin", _FakeStdin(json.dumps(parsed).encode()))
        monkeypatch.setattr(sys, "stdout", to_yaml_stdout)
        to_yaml_cmd = object.__new__(JsonToYaml)
        await JsonToYaml.run(to_yaml_cmd)
        assert "mode: 420" in to_yaml_stdout.text


def _stub_deploy(monkeypatch: pytest.MonkeyPatch, *, no_verify: bool) -> tuple[Deploy, list[str]]:
    """Build a Deploy with every step it orchestrates replaced by a recorder.

    Deploy.run resolves the GitOps branches/files itself and then finalizes
    the commit through `_finalize_commit` -- it does not delegate to
    `Commit.run` -- so both of those are stubbed too. `sourceBranch` is None
    here, which is what keeps `prepare_deploy_and_source_commits` (and thus
    any real git repository) out of the picture.
    """
    calls: list[str] = []

    async def verify(_: object) -> None:
        calls.append("verify")

    async def push_cache(*_args: object, **_kwargs: object) -> None:
        calls.append("push_cache")

    async def resolve_gitops(*_args: object) -> tuple[str, None, list[tuple[str, str]]]:
        return "deploy", None, [("default/ConfigMap/my-config.yaml", "kind: ConfigMap\n")]

    async def finalize_commit(*_args: object, **_kwargs: object) -> None:
        calls.append("commit")

    async def jj_status(*_args: object) -> None:
        return None

    monkeypatch.setattr(Validate, "run", verify)
    monkeypatch.setattr("ekn.cli._push_ekn_cache", push_cache)
    monkeypatch.setattr("ekn.cli._resolve_gitops", resolve_gitops)
    monkeypatch.setattr("ekn.cli._finalize_commit", finalize_commit)
    monkeypatch.setattr("ekn.cli.try_jj_status", jj_status)

    deploy = object.__new__(Deploy)
    deploy.no_verify = no_verify
    deploy.flake = ".#test"
    deploy.file = None
    deploy.attr = None
    deploy.cache_allow_failure = False
    deploy.message = "test"
    deploy.push = False
    deploy.remote = "origin"
    deploy.verbosity = "error"
    deploy.print_build_logs = False
    return deploy, calls


class TestCommit:
    async def test_deploy_verifies_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        deploy, calls = _stub_deploy(monkeypatch, no_verify=False)

        await Deploy.run(deploy)

        assert calls == ["verify", "push_cache", "commit"]

    async def test_deploy_no_verify_skips_validation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        deploy, calls = _stub_deploy(monkeypatch, no_verify=True)

        await Deploy.run(deploy)

        assert calls == ["push_cache", "commit"]

    async def test_first_commit(self, tmp_path: Path, git_repo: Path) -> None:
        f = tmp_path / "customers.nix"
        f.write_text(CUSTOMER_NIX)
        os.environ["EKN_REPO"] = str(git_repo)
        try:
            result = await evaluate_file(f, "customer1")
            files = flatten_manifests(result["config"]["kubernetes"]["generated"])
            commit_id = commit_manifests(".", result["config"]["gitops"]["branch"], files, "test")
            assert isinstance(commit_id, str)
            assert len(commit_id) > 0
        finally:
            os.environ.pop("EKN_REPO", None)

    async def test_second_commit(self, tmp_path: Path, git_repo: Path) -> None:
        f = tmp_path / "customers.nix"
        f.write_text(CUSTOMER_NIX)
        os.environ["EKN_REPO"] = str(git_repo)
        try:
            result = await evaluate_file(f, "customer1")
            files = flatten_manifests(result["config"]["kubernetes"]["generated"])
            commit_manifests(".", result["config"]["gitops"]["branch"], files, "first")
            commit_id = commit_manifests(".", result["config"]["gitops"]["branch"], files, "second")
            assert isinstance(commit_id, str)
        finally:
            os.environ.pop("EKN_REPO", None)

    async def test_commit_branch_override(self, tmp_path: Path, git_repo: Path) -> None:
        f = tmp_path / "customers.nix"
        f.write_text(CUSTOMER_NIX)
        os.environ["EKN_REPO"] = str(git_repo)
        try:
            result = await evaluate_file(f, "customer1")
            files = flatten_manifests(result["config"]["kubernetes"]["generated"])
            commit_id = commit_manifests(".", "override", files, "override")
            assert isinstance(commit_id, str)
        finally:
            os.environ.pop("EKN_REPO", None)


EXAMPLE_FLAKE = str((Path(__file__).resolve().parent.parent / "docs/examples/example-flake").resolve())


class TestFlakeEval:
    async def test_flake_eval(self) -> None:
        result = await evaluate_flake_ekn(EXAMPLE_FLAKE, "myapp")
        namespaces = {obj["metadata"]["namespace"] for obj in result.config.kubernetes.generated}
        assert "default" in namespaces

    async def test_flake_diff(self, git_repo: Path) -> None:
        os.environ["EKN_REPO"] = str(git_repo)
        try:
            result = await evaluate_flake_ekn(EXAMPLE_FLAKE, "myapp")
            files = flatten_manifests(result.config.kubernetes.generated)
            commit_manifests(".", "flake-test", files, "seed")
            diff_out = diff_manifests(".", "flake-test", files)
            assert diff_out is None
        finally:
            os.environ.pop("EKN_REPO", None)

    async def test_flake_commit(self, git_repo: Path) -> None:
        os.environ["EKN_REPO"] = str(git_repo)
        try:
            result = await evaluate_flake_ekn(EXAMPLE_FLAKE, "myapp")
            files = flatten_manifests(result.config.kubernetes.generated)
            commit_id = commit_manifests(".", "flake-test", files, "flake-test")
            assert isinstance(commit_id, str)
        finally:
            os.environ.pop("EKN_REPO", None)


class TestCachePushTimeout:
    """`ekn.cacheTimeoutSec` -- see easykubenix issue #20.

    A `cacheTo` host that is routed but not listening drops the connect
    rather than refusing it, and ssh waits that out in silence. The push has
    a deadline for that reason, and these cover what the deadline does when
    it expires rather than whether anyio's `fail_after` works.
    """

    @staticmethod
    def _stub(
        monkeypatch: pytest.MonkeyPatch,
        *,
        timeout_sec: float | None,
        push: object,
    ) -> list[float | None]:
        """Point `_push_ekn_cache` at a config with `timeout_sec` and a
        recording push. Returns the list the timeout is recorded into."""
        seen: list[float | None] = []

        async def cache_config(*_args: object, **_kwargs: object) -> CacheConfigResult:
            return CacheConfigResult(
                cache_to=["ssh-ng://nix@unreachable.invalid"],
                cache_paths=["/nix/store/deadbeefdeadbeefdeadbeefdeadbeef-manifest.json"],
                cache_timeout_sec=timeout_sec,
            )

        async def push_closure(*_args: object, timeout_sec: float | None = None, **_kwargs: object) -> None:
            seen.append(timeout_sec)
            await push()  # type: ignore[operator]

        monkeypatch.setattr("ekn.cli.evaluate_cache_config", cache_config)
        monkeypatch.setattr("ekn.cli.push_closure_to_store", push_closure)
        return seen

    async def test_timeout_reaches_the_push(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fine() -> None:
            return None

        seen = self._stub(monkeypatch, timeout_sec=42, push=fine)

        await _push_ekn_cache(None, ".#test", None, allow_failure=False)

        assert seen == [42]

    async def test_timeout_stops_the_deploy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def slow() -> None:
            raise TimeoutError

        self._stub(monkeypatch, timeout_sec=1, push=slow)

        with pytest.raises(SystemExit) as exc:
            await _push_ekn_cache(None, ".#test", None, allow_failure=False)
        assert exc.value.code == 1

    async def test_allow_failure_covers_a_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The flag's promise is "log a warning and continue". A timeout is
        a failure of the push like any other, so it is covered too."""

        async def slow() -> None:
            raise TimeoutError

        self._stub(monkeypatch, timeout_sec=1, push=slow)

        await _push_ekn_cache(None, ".#test", None, allow_failure=True)

    async def test_null_timeout_waits_forever(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fine() -> None:
            return None

        seen = self._stub(monkeypatch, timeout_sec=None, push=fine)

        await _push_ekn_cache(None, ".#test", None, allow_failure=False)

        assert seen == [None]

    async def test_the_option_reaches_the_config(self, tmp_path: Path) -> None:
        """The chain unstubbed: `ekn.cacheTimeoutSec` as written, through
        `evaluate_cache_config`, into the field `_push_ekn_cache` reads. A
        default that never reached Python would leave the deploy exactly as
        unbounded as it was before.

        A real instance, so the option's type and its place in the tree are
        part of what this covers -- a hand-written `{ config.ekn = ...; }`
        would answer for neither.
        """
        sources_path = PROJECT_ROOT / "nix/sources.nix"
        f = tmp_path / "instance.nix"
        f.write_text(f"""
            let
              sources = import {sources_path};
              pkgs = import sources.nixpkgs {{ }};
            in
            import {PROJECT_ROOT} {{
              inherit pkgs;
              modules = [
                {{
                  ekn.environment = "easykubenix";
                  ekn.cacheTo = "ssh-ng://nix@example.invalid";
                  ekn.cacheTimeoutSec = 7;
                  kubernetes.objects.default.ConfigMap.c.data.key = "value";
                }}
              ];
            }}
        """)

        cfg = await evaluate_cache_config(f, None, None, None)

        assert cfg.cache_timeout_sec == 7
        # A bare string in Nix, a one-element list here: normalised at the
        # boundary so nothing downstream branches on the shape.
        assert cfg.cache_to == ["ssh-ng://nix@example.invalid"]
        # Left unset above, so this is the default an instance that never
        # thought about host keys deploys with. Issue #18.
        assert cfg.cache_accept_new_host_keys is True

    async def test_accept_new_host_keys_can_be_turned_off(self, tmp_path: Path) -> None:
        """Where the host keys arrive ahead of time, for example from NixOS
        `programs.ssh.knownHosts`, an unknown one is a real failure."""
        sources_path = PROJECT_ROOT / "nix/sources.nix"
        f = tmp_path / "strict.nix"
        f.write_text(f"""
            let
              sources = import {sources_path};
              pkgs = import sources.nixpkgs {{ }};
            in
            import {PROJECT_ROOT} {{
              inherit pkgs;
              modules = [
                {{
                  ekn.environment = "easykubenix";
                  ekn.cacheTo = "ssh-ng://nix@example.invalid";
                  ekn.cacheAcceptNewHostKeys = false;
                  kubernetes.objects.default.ConfigMap.c.data.key = "value";
                }}
              ];
            }}
        """)

        cfg = await evaluate_cache_config(f, None, None, None)

        assert cfg.cache_accept_new_host_keys is False

    async def test_a_path_whose_context_was_stripped_is_still_pushed(self, tmp_path: Path) -> None:
        """The reason the push reads the manifest's text rather than its Nix
        string context.

        `unsafeDiscardStringContext` is exactly what nixkube's
        `discardStringContext` does to every `nixkube/discard` resource, and
        the node and pynixd environments are those resources. A push that
        followed context did not have them in its set at all, and reported
        success having moved nothing a node needs.
        """
        sources_path = PROJECT_ROOT / "nix/sources.nix"
        f = tmp_path / "stripped.nix"
        f.write_text(f"""
            let
              sources = import {sources_path};
              pkgs = import sources.nixpkgs {{ }};
            in
            import {PROJECT_ROOT} {{
              inherit pkgs;
              modules = [
                {{
                  ekn.environment = "easykubenix";
                  ekn.cacheTo = "ssh-ng://nix@example.invalid";
                  kubernetes.objects.default.ConfigMap.c.data.stripped =
                    builtins.unsafeDiscardStringContext "${{pkgs.hello}}";
                }}
              ];
            }}
        """)

        cfg = await evaluate_cache_config(f, None, None, None)

        hello = [path for path in cfg.cache_paths if path.endswith("-hello-2.12.3")]
        assert hello, f"the stripped path is missing from {cfg.cache_paths}"

    async def test_a_list_of_destinations_is_kept_in_order(self, tmp_path: Path) -> None:
        """One destination cannot serve a path whose consumer *is* that
        destination -- nixlab2's `cacheTo` is pynixd, and pynixd's own
        environment is mounted over CSI on a node."""
        sources_path = PROJECT_ROOT / "nix/sources.nix"
        f = tmp_path / "multi.nix"
        f.write_text(f"""
            let
              sources = import {sources_path};
              pkgs = import sources.nixpkgs {{ }};
            in
            import {PROJECT_ROOT} {{
              inherit pkgs;
              modules = [
                {{
                  ekn.environment = "easykubenix";
                  ekn.cacheTo = [ "ssh-ng://nix@pynixd" "https://nixkube.cachix.org" ];
                  kubernetes.objects.default.ConfigMap.c.data.key = "value";
                }}
              ];
            }}
        """)

        cfg = await evaluate_cache_config(f, None, None, None)

        assert cfg.cache_to == ["ssh-ng://nix@pynixd", "https://nixkube.cachix.org"]

    async def test_no_cache_to_is_an_empty_list(self, tmp_path: Path) -> None:
        """Not `None`. The push loop asks `if not cfg.cache_to`, so one
        falsy shape rather than two is one branch nobody can forget."""
        sources_path = PROJECT_ROOT / "nix/sources.nix"
        f = tmp_path / "nocache.nix"
        f.write_text(f"""
            let
              sources = import {sources_path};
              pkgs = import sources.nixpkgs {{ }};
            in
            import {PROJECT_ROOT} {{
              inherit pkgs;
              modules = [
                {{
                  ekn.environment = "easykubenix";
                  kubernetes.objects.default.ConfigMap.c.data.key = "value";
                }}
              ];
            }}
        """)

        cfg = await evaluate_cache_config(f, None, None, None)

        assert cfg.cache_to == []


class TestAWholeInstancePruneNeedsItsExclusionSet:
    """A prune that does not know what to leave alone must not run.

    `deployment.handAppliedUnits` is what keeps a whole-instance `--prune`
    away from a bootstrap unit's objects -- ArgoCD, the CNI -- which carry
    the environment label and are never in a whole-instance apply's desired
    set. An easykubenix older than that option evaluates to no list at all,
    and proceeding with an empty one would delete every one of them on the
    first run.

    Unknown and empty must therefore stay distinguishable: empty is the
    ordinary shape of a configuration with no nested units.
    """

    @staticmethod
    def _config(hand_applied: list[str] | None) -> KubeApplyConfigResult:
        return KubeApplyConfigResult.model_validate(
            {
                "groups": [{"unit": None, "field_manager": "ekn", "objects": []}],
                "environment": "test",
                "resource_priority": {},
                "sops_age_identities": [],
                "handAppliedUnits": hand_applied,
                "declaredUnits": hand_applied,
            }
        )

    async def test_an_unknown_exclusion_set_refuses_the_prune(self) -> None:
        with pytest.raises(SystemExit, match="handAppliedUnits"):
            await _apply_groups(self._config(None), api=None, target=None, prune=True)  # type: ignore[arg-type]

    async def test_no_hand_applied_units_is_not_the_same_as_unknown(self) -> None:
        """The empty list runs. A guard that refused it would make `--prune`
        useless for every configuration that declares no nested unit, which
        is most of them."""
        await _apply_groups(self._config([]), api=None, target=None, prune=True)  # type: ignore[arg-type]

    async def test_a_target_apply_is_unaffected(self) -> None:
        """A `--target` prune scopes by the unit's own label and never reads
        the exclusion set, so an old configuration can still use it -- which
        is what the refusal message offers as the way forward."""
        await _apply_groups(self._config(None), api=None, target="bootstrap", prune=True)  # type: ignore[arg-type]


class TestAWholeInstanceConvergingApply:
    """`ekn kubeapply --converge`, with no `--target`, converges everything.

    The question this answers is "which targets are stale?", and the answer
    has to be "none, by construction" rather than a clusterdiff per target.
    A whole-instance apply is one group with `unit: None` holding the entire
    `kubernetes.generated` set, so this is the path that covers every routed
    unit in one run.
    """

    @staticmethod
    def _config() -> KubeApplyConfigResult:
        return KubeApplyConfigResult.model_validate(
            {
                "groups": [
                    {
                        "unit": None,
                        "field_manager": "ekn",
                        "objects": [
                            {
                                "apiVersion": "v1",
                                "kind": "ConfigMap",
                                "metadata": {"name": "a", "namespace": "default"},
                            }
                        ],
                    }
                ],
                "environment": "test",
                "resource_priority": {},
                "sops_age_identities": [],
                "handAppliedUnits": ["bootstrap"],
                "declaredUnits": ["bootstrap"],
            }
        )

    async def test_it_converges_instead_of_walking_barriers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import ekn.cli as cli_module

        seen: dict[str, Any] = {}

        async def _converge_direct(objects: Any, **kwargs: Any) -> Any:
            seen["objects"] = objects
            seen["field_manager"] = kwargs["field_manager"]
            from ekn.converge import ConvergeReport

            return ConvergeReport(applied=len(objects), skipped=0, failures=[]), {}

        async def _apply_and_prune(*_args: Any, **_kwargs: Any) -> None:
            seen["barriers"] = True

        monkeypatch.setattr(cli_module, "converge_direct", _converge_direct)
        monkeypatch.setattr(cli_module, "apply_and_prune", _apply_and_prune)

        await _apply_groups(
            self._config(),
            api=None,  # type: ignore[arg-type]
            target=None,
            prune=False,
            converge=cli_module._ConvergeOptions(enabled=True),
        )

        assert "barriers" not in seen, "converge mode must not fall through to the barrier apply"
        assert [o["metadata"]["name"] for o in seen["objects"]] == ["a"]

    async def test_without_the_flag_it_still_walks_barriers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Negative control: the converging path is opt-in, so an existing
        `ekn kubeapply` keeps the behaviour it had."""
        import ekn.cli as cli_module

        seen: dict[str, Any] = {}

        async def _apply_and_prune(*_args: Any, **_kwargs: Any) -> None:
            seen["barriers"] = True

        monkeypatch.setattr(cli_module, "apply_and_prune", _apply_and_prune)

        await _apply_groups(self._config(), api=None, target=None, prune=False)  # type: ignore[arg-type]

        assert seen == {"barriers": True}


class TestTheEnginePauseHoldsObjectsBack:
    """A paused engine's own workloads are neither applied nor pruned.

    Applying one re-applies its replica count and wakes the engine in the
    middle of the apply that paused it; skipping it without protecting it
    makes it absent from the desired set, which a prune answers with a
    delete. Both halves or neither. Issue Lillecarl/easykubenix#28.
    """

    CONTROLLER: ClassVar[dict[str, Any]] = {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": {"namespace": "argocd", "name": "argo-cd-argocd-application-controller"},
        "spec": {"replicas": 1},
    }
    ORDINARY: ClassVar[dict[str, Any]] = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"namespace": "argocd", "name": "unrelated"},
    }

    @classmethod
    def _config(cls) -> KubeApplyConfigResult:
        return KubeApplyConfigResult.model_validate(
            {
                "groups": [{"unit": None, "field_manager": "ekn", "objects": [cls.CONTROLLER, cls.ORDINARY]}],
                "environment": "test",
                "resource_priority": {},
                "sops_age_identities": [],
                "handAppliedUnits": [],
                "declaredUnits": [],
            }
        )

    async def test_the_paused_workload_is_not_applied_and_is_protected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import ekn.cli as cli_module

        seen: dict[str, Any] = {}

        async def _apply_and_prune(objects: Any, **kwargs: Any) -> None:
            seen["objects"] = objects
            seen["protect"] = kwargs["protect"]

        monkeypatch.setattr(cli_module, "apply_and_prune", _apply_and_prune)
        held = {("argocd", "StatefulSet", "argo-cd-argocd-application-controller")}

        await _apply_groups(
            self._config(),
            api=None,  # type: ignore[arg-type]
            target=None,
            prune=False,
            held_by_engine_pause=held,
        )

        assert [o["metadata"]["name"] for o in seen["objects"]] == ["unrelated"]
        assert held <= seen["protect"], "a skipped object absent from protect is one the prune deletes"

    async def test_without_a_pause_everything_is_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Negative control: holding objects back is what `--pause-engine`
        buys, and must not happen without it."""
        import ekn.cli as cli_module

        seen: dict[str, Any] = {}

        async def _apply_and_prune(objects: Any, **_kwargs: Any) -> None:
            seen["objects"] = objects

        monkeypatch.setattr(cli_module, "apply_and_prune", _apply_and_prune)

        await _apply_groups(self._config(), api=None, target=None, prune=False)  # type: ignore[arg-type]

        assert len(seen["objects"]) == 2


class TestTheEngineIsResumedWhateverHappens:
    """A pause that cannot be resumed is worse than no pause."""

    async def test_a_failing_apply_still_resumes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The case that matters. An apply that raises must not leave the
        cluster with no reconciler and nobody watching."""
        import ekn.cli as cli_module

        resumed: list[Any] = []

        async def _pause(workloads: Any, **_kwargs: Any) -> list[Any]:
            return [cli_module.enginepause.Paused(w, 1) for w in workloads]

        async def _resume(paused: Any, **_kwargs: Any) -> list[str]:
            resumed.extend(entry.workload for entry in paused)
            return []

        monkeypatch.setattr(cli_module.enginepause, "pause", _pause)
        monkeypatch.setattr(cli_module.enginepause, "resume", _resume)
        workload = cli_module.enginepause.Workload(namespace="argocd", name="c", kind="StatefulSet")

        with pytest.raises(RuntimeError, match="the apply blew up"):
            async with cli_module._engine_paused([workload], api=None):  # type: ignore[arg-type]
                raise RuntimeError("the apply blew up")

        assert resumed == [workload]

    async def test_a_failed_resume_is_its_own_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Not an apply failure. The apply may have succeeded; what needs
        attention is the cluster having no reconciler."""
        import ekn.cli as cli_module

        async def _pause(workloads: Any, **_kwargs: Any) -> list[Any]:
            return [cli_module.enginepause.Paused(w, 1) for w in workloads]

        async def _resume(_paused: Any, **_kwargs: Any) -> list[str]:
            return ["StatefulSet argocd/c: nope"]

        monkeypatch.setattr(cli_module.enginepause, "pause", _pause)
        monkeypatch.setattr(cli_module.enginepause, "resume", _resume)
        workload = cli_module.enginepause.Workload(namespace="argocd", name="c", kind="StatefulSet")

        with pytest.raises(cli_module.EngineNotResumedError, match="not fully resumed"):
            async with cli_module._engine_paused([workload], api=None):  # type: ignore[arg-type]
                pass


class TestTheCachePushIsSharedNotCopied:
    """`ekn deploy` and `ekn kubeapply` push for the same reason, so they
    declare it once. Two copies of a flag drift: one grows a default the
    other lacks, and the help text stops agreeing with the behaviour.
    """

    def test_both_commands_offer_the_same_switches(self) -> None:
        from ekn.cli import Deploy, KubeApply

        for cls in (Deploy, KubeApply):
            assert "cache_push" in cls.specs, cls.__name__
            assert "cache_allow_failure" in cls.specs, cls.__name__

    def test_both_commands_hold_the_same_declaration_object(self) -> None:
        """Identity, not equality, and that is the point of the test.

        Equality passes a verbatim copy, which is the thing being forbidden
        -- a copy is fine on the day it is made and drifts afterwards.
        Identity fails the moment either command redeclares the flag in its
        own body, whatever it writes there.
        """
        from ekn.cli import Deploy, KubeApply

        for name in ("cache_push", "cache_allow_failure"):
            assert Deploy.specs[name] is KubeApply.specs[name], name

    def test_commit_does_not_push(self) -> None:
        """Negative control. `ekn commit` writes branches and pushes nothing
        to a store, so inheriting the flag would offer a switch that does
        nothing."""
        from ekn.cli import Commit

        assert "cache_push" not in Commit.specs
        assert "cache_allow_failure" not in Commit.specs

    def test_deploy_still_has_everything_commit_declares(self) -> None:
        """`Deploy(CachePushCommand, Commit)` is multiple inheritance, and
        the option collection walks the MRO -- so this asserts the second
        base did not fall out of it."""
        from ekn.cli import Commit, Deploy

        assert not set(Commit.specs) - set(Deploy.specs)


class TestThePythonProfiler:
    """`EKN_PROFILE` closes the one blind spot left in a render's timing.

    Measured on a full nixlab2 render: 11-12s wall, `to_python(kubernetes.
    generated)` 8.8-9.6s, and the Nix evaluator only 5.4s of it. So roughly
    4s is IFD realisation and marshalling 919 objects, which `EKN_EVAL_
    PROFILER` cannot see and `EKN_TIMING` can only name.
    """

    def test_it_writes_a_profile_that_pstats_can_read(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import pstats

        from ekn.eval import python_profile

        destination = tmp_path / "ekn.profile"
        monkeypatch.setenv("EKN_PROFILE", "1")
        monkeypatch.setenv("EKN_PROFILE_FILE", str(destination))

        with python_profile():
            sum(range(10_000))

        # Readable afterwards is the whole point: a dump no tool can open
        # would pass a "the file exists" assertion and answer nothing.
        assert pstats.Stats(str(destination)).stats  # type: ignore[attr-defined]

    def test_it_is_off_unless_asked(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Negative control. Profiling every run would slow the ordinary
        path and litter the working tree with `ekn.profile`."""
        from ekn.eval import python_profile

        destination = tmp_path / "ekn.profile"
        monkeypatch.delenv("EKN_PROFILE", raising=False)
        monkeypatch.setenv("EKN_PROFILE_FILE", str(destination))

        with python_profile():
            pass

        assert not destination.exists()

    def test_an_exception_still_writes_the_profile(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A run that fails is one of the runs worth profiling, and the
        `finally` is what makes the slow-then-failed case readable."""
        from ekn.eval import python_profile

        destination = tmp_path / "ekn.profile"
        monkeypatch.setenv("EKN_PROFILE", "1")
        monkeypatch.setenv("EKN_PROFILE_FILE", str(destination))

        with pytest.raises(RuntimeError, match="boom"), python_profile():
            raise RuntimeError("boom")

        assert destination.exists()


class TestTheExitCode:
    """A command that fails must not report success.

    Issue #21: `ekn` printed a traceback and exited **0**. A `&&` chain then
    continued against infrastructure nothing had touched, and one `ekn deploy
    --push` aborted before commit and push while the GitOps branches still
    pointed at a destroyed cluster's render -- reported as a success.

    It does not reproduce on this tree. These are the guard, because the
    defect is silent in the dangerous direction and nothing asserted the
    invariant.

    A subprocess, and not `pytest.raises(SystemExit)`. The claim is about the
    **exit code of the process**, and an in-process check cannot see the step
    that was wrong: everything up to `sys.exit` was already correct.
    """

    def _run(self, body: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", body],
            capture_output=True,
            text=True,
            check=False,
            cwd=PROJECT_ROOT,
        )

    def test_an_unhandled_exception_exits_non_zero(self) -> None:
        """The general case, and the one the issue asks for."""
        done = self._run(
            "import sys\n"
            "from ekn import cli\n"
            "class Boom:\n"
            "    async def run(self):\n"
            "        raise RuntimeError('aborted before commit and push')\n"
            "cli.dispatch = lambda parser, args: Boom()\n"
            "sys.argv = ['ekn', 'deploy']\n"
            "cli.main()\n"
        )

        assert done.returncode != 0, done.stdout + done.stderr
        assert "aborted before commit and push" in done.stderr

    def test_a_failure_before_the_command_runs_exits_non_zero(self) -> None:
        """The first case of the issue: `-A` with no `--file` or `--flake`.

        It raises out of `dispatch`, which is before `main` installs the
        traceback handler at all.
        """
        done = self._run(
            "import sys\n"
            "from ekn import cli\n"
            "sys.argv = ['ekn', '-A', 'environments.nixlab2', 'tofu', 'output',"
            " '--target', 'x', 'y']\n"
            "cli.main()\n"
        )

        assert done.returncode != 0, done.stdout + done.stderr

    def test_success_still_exits_zero(self) -> None:
        """Without this the two above pass on a command that always fails."""
        done = self._run(
            "import sys\n"
            "from ekn import cli\n"
            "class Fine:\n"
            "    async def run(self):\n"
            "        return None\n"
            "cli.dispatch = lambda parser, args: Fine()\n"
            "sys.argv = ['ekn', 'deploy']\n"
            "cli.main()\n"
        )

        assert done.returncode == 0, done.stdout + done.stderr
