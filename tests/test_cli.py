from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ekn.cli import Deploy, JsonToYaml, Validate, YamlToJson
from ekn.eval import evaluate_file, evaluate_flake_ekn
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
