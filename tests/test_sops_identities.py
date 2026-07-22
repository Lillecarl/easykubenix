from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import kr8s
import pytest
import yaml

from ekn.sops import (
    SopsUpdateKeysError,
    _add_recipient_to_sops_config,
    ensure_age_identities,
)


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


class _FakeResponse:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def json(self) -> dict[str, Any]:
        return self._data


class FakeApi:
    """Stands in for kr8s's real Api -- mirrors tests/test_clusterdiff.py's
    FakeApi (answers GET/PATCH calls from a canned script) so this doesn't
    need a real cluster."""

    namespace = "default"

    def __init__(self, script: dict[tuple[str, str], Any]) -> None:
        self._script = script
        self.calls: list[tuple[str, str]] = []

    @asynccontextmanager
    async def call_api(
        self,
        method: str,
        *,
        version: str | None = None,
        url: str | None = None,
        namespace: str | None = None,
        content: str | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ):
        key = (method, url or "")
        self.calls.append(key)
        result = self._script[key]
        if isinstance(result, Exception):
            raise result
        yield _FakeResponse(result)

    async def async_lookup_kind(self, lookup: str) -> tuple[str, str, bool]:
        raise ValueError(f"Kind {lookup.split('.', 1)[0].lower()} not found.")


def _not_found() -> kr8s.ServerError:
    response = httpx.Response(404, request=httpx.Request("GET", "http://x/"))
    return kr8s.ServerError("not found", response=response)


def _secret(name: str, namespace: str, key_text: str | None = None, key_name: str = "key.txt") -> dict[str, Any]:
    spec: dict[str, Any] = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": namespace},
    }
    if key_text is not None:
        spec["data"] = {key_name: base64.b64encode(key_text.encode()).decode()}
    return spec


def _namespace(name: str) -> dict[str, Any]:
    return {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name}}


@pytest.fixture
def age_identity_text(tmp_path: Path) -> str:
    key_file = tmp_path / "identity.txt"
    subprocess.run(["age-keygen", "-o", str(key_file)], check=True, capture_output=True)
    return key_file.read_text()


class TestEnsureAgeIdentities:
    async def test_skips_an_identity_whose_secret_already_exists(
        self, age_identity_text: str
    ) -> None:
        api = FakeApi(
            {("GET", "secrets/sops-age-key"): _secret("sops-age-key", "argocd", age_identity_text)}
        )

        await ensure_age_identities(
            [{"namespace": "argocd", "secretName": "sops-age-key"}], api=api
        )  # type: ignore[arg-type]

        assert api.calls == [("GET", "secrets/sops-age-key")]

    async def test_creates_namespace_and_secret_when_missing(self) -> None:
        api = FakeApi(
            {
                ("GET", "secrets/sops-age-key"): _not_found(),
                ("PATCH", "namespaces/argocd"): _namespace("argocd"),
                ("PATCH", "secrets/sops-age-key"): _secret("sops-age-key", "argocd"),
            }
        )

        await ensure_age_identities(
            [{"namespace": "argocd", "secretName": "sops-age-key"}], api=api
        )  # type: ignore[arg-type]

        assert ("PATCH", "namespaces/argocd") in api.calls
        assert ("PATCH", "secrets/sops-age-key") in api.calls

    async def test_default_key_name_is_key_txt(self) -> None:
        captured: dict[str, Any] = {}

        class _CapturingApi(FakeApi):
            @asynccontextmanager
            async def call_api(self, method: str, *, url: str | None = None, content: str | None = None, **kwargs: Any):
                if method == "PATCH" and url == "secrets/sops-age-key":
                    captured["body"] = json.loads(content or "{}")
                async with super().call_api(method, url=url, content=content, **kwargs) as resp:
                    yield resp

        api = _CapturingApi(
            {
                ("GET", "secrets/sops-age-key"): _not_found(),
                ("PATCH", "namespaces/argocd"): _namespace("argocd"),
                ("PATCH", "secrets/sops-age-key"): _secret("sops-age-key", "argocd"),
            }
        )

        await ensure_age_identities(
            [{"namespace": "argocd", "secretName": "sops-age-key"}], api=api
        )  # type: ignore[arg-type]

        assert set(captured["body"]["stringData"].keys()) == {"key.txt"}
        assert "AGE-SECRET-KEY" in captured["body"]["stringData"]["key.txt"]

    async def test_registers_recipient_and_updates_keys_end_to_end(
        self, tmp_path: Path, age_key: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Full flow with a real .sops.yaml and a real sops-encrypted file:
        after ensure_age_identities runs, the new identity's public key must
        both be listed in .sops.yaml *and* actually be able to decrypt the
        file (not just be listed -- sops updatekeys must have really run).

        `sops updatekeys` needs to decrypt the file with an *existing*
        recipient before it can rewrap it for the new one -- monkeypatch,
        not a local `env` dict, since ensure_age_identities' own subprocess
        call inherits the real process environment.
        """
        monkeypatch.setenv("SOPS_AGE_KEY_FILE", str(age_key))
        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir()
        encrypted_file = secrets_dir / "all.yaml"
        # Encrypted in-place as genuine YAML (matching how a real
        # secrets/all.yaml gets created: `sops --encrypt --in-place`),
        # not JSON-over-stdin like test_sops.py's helper -- `sops
        # updatekeys` re-serializes by the file's own extension regardless
        # of how it was originally written, so a YAML-named file must
        # actually be YAML for this round-trip to be representative.
        encrypted_file.write_text("password: hunter2\n")
        encrypt_proc = await asyncio.create_subprocess_exec(
            "sops", "--encrypt", "--in-place", "--age", _public_key_from_original(age_key), str(encrypted_file),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, encrypt_stderr = await encrypt_proc.communicate()
        assert encrypt_proc.returncode == 0, encrypt_stderr.decode()

        config_file = tmp_path / ".sops.yaml"
        config_file.write_text(
            yaml.safe_dump(
                {
                    "creation_rules": [
                        {
                            "path_regex": ".*secrets/.*\\.yaml$",
                            "age": _public_key_from_original(age_key),
                        }
                    ]
                }
            )
        )

        captured: dict[str, Any] = {}

        class _CapturingApi(FakeApi):
            @asynccontextmanager
            async def call_api(self, method: str, *, url: str | None = None, content: str | None = None, **kwargs: Any):
                if method == "PATCH" and url == "secrets/sops-age-key":
                    captured["body"] = json.loads(content or "{}")
                async with super().call_api(method, url=url, content=content, **kwargs) as resp:
                    yield resp

        api = _CapturingApi(
            {
                ("GET", "secrets/sops-age-key"): _not_found(),
                ("PATCH", "namespaces/argocd"): _namespace("argocd"),
                ("PATCH", "secrets/sops-age-key"): _secret("sops-age-key", "argocd"),
            }
        )

        await ensure_age_identities(
            [
                {
                    "namespace": "argocd",
                    "secretName": "sops-age-key",
                    "sopsConfigFile": str(config_file),
                    "sopsFiles": [str(encrypted_file)],
                }
            ],
            api=api,
        )  # type: ignore[arg-type]

        updated_config = yaml.safe_load(config_file.read_text())
        recipients = updated_config["creation_rules"][0]["age"]
        assert _public_key_from_original(age_key) in recipients
        # A second recipient was actually added, not just left as-is.
        assert recipients.count(",") == 1

        # Decrypting with the *new* identity (the one ensure_age_identities
        # actually generated, captured straight from the Secret it created)
        # only works if sops updatekeys really rewrapped the data key --
        # this is the check that catches "recipient listed but never
        # applied" bugs that a .sops.yaml-only assertion would miss.
        new_identity_text = captured["body"]["stringData"]["key.txt"]
        new_key_file = tmp_path / "new-identity.txt"
        new_key_file.write_text(new_identity_text)
        decrypt_proc = await asyncio.create_subprocess_exec(
            "sops", "--decrypt", str(encrypted_file),
            env=os.environ | {"SOPS_AGE_KEY_FILE": str(new_key_file)},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        decrypt_stdout, decrypt_stderr = await decrypt_proc.communicate()
        assert decrypt_proc.returncode == 0, decrypt_stderr.decode()
        assert yaml.safe_load(decrypt_stdout) == {"password": "hunter2"}

    async def test_raises_when_sops_updatekeys_fails(self, tmp_path: Path) -> None:
        config_file = tmp_path / ".sops.yaml"
        config_file.write_text(
            yaml.safe_dump({"creation_rules": [{"path_regex": ".*\\.yaml$", "age": ""}]})
        )
        missing_file = tmp_path / "does-not-exist.yaml"

        api = FakeApi(
            {
                ("GET", "secrets/sops-age-key"): _not_found(),
                ("PATCH", "namespaces/argocd"): _namespace("argocd"),
                ("PATCH", "secrets/sops-age-key"): _secret("sops-age-key", "argocd"),
            }
        )

        with pytest.raises(SopsUpdateKeysError):
            await ensure_age_identities(
                [
                    {
                        "namespace": "argocd",
                        "secretName": "sops-age-key",
                        "sopsConfigFile": str(config_file),
                        "sopsFiles": [str(missing_file)],
                    }
                ],
                api=api,
            )  # type: ignore[arg-type]


class TestAddRecipientToSopsConfig:
    def test_adds_recipient_to_matching_rule(self, tmp_path: Path) -> None:
        config_file = tmp_path / ".sops.yaml"
        config_file.write_text(
            yaml.safe_dump({"creation_rules": [{"path_regex": ".*secrets/.*\\.yaml$", "age": "age1existing"}]})
        )
        (tmp_path / "secrets").mkdir()
        sops_file = tmp_path / "secrets" / "all.yaml"

        changed = _add_recipient_to_sops_config(str(config_file), [str(sops_file)], "age1new")

        assert changed is True
        updated = yaml.safe_load(config_file.read_text())
        assert updated["creation_rules"][0]["age"] == "age1existing,age1new"

    def test_is_idempotent_when_recipient_already_present(self, tmp_path: Path) -> None:
        config_file = tmp_path / ".sops.yaml"
        config_file.write_text(
            yaml.safe_dump({"creation_rules": [{"path_regex": ".*secrets/.*\\.yaml$", "age": "age1new"}]})
        )
        (tmp_path / "secrets").mkdir()
        sops_file = tmp_path / "secrets" / "all.yaml"

        changed = _add_recipient_to_sops_config(str(config_file), [str(sops_file)], "age1new")

        assert changed is False

    def test_leaves_non_matching_rules_untouched(self, tmp_path: Path) -> None:
        config_file = tmp_path / ".sops.yaml"
        config_file.write_text(
            yaml.safe_dump({"creation_rules": [{"path_regex": ".*other/.*\\.yaml$", "age": "age1existing"}]})
        )
        (tmp_path / "secrets").mkdir()
        sops_file = tmp_path / "secrets" / "all.yaml"

        changed = _add_recipient_to_sops_config(str(config_file), [str(sops_file)], "age1new")

        assert changed is False
        updated = yaml.safe_load(config_file.read_text())
        assert updated["creation_rules"][0]["age"] == "age1existing"


@pytest.fixture
def age_key(tmp_path: Path) -> Path:
    key_file = tmp_path / "key.txt"
    subprocess.run(["age-keygen", "-o", str(key_file)], check=True, capture_output=True)
    return key_file


def _public_key_from_original(key_file: Path) -> str:
    for line in key_file.read_text().splitlines():
        if line.startswith("# public key:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"no public key comment in {key_file}")


