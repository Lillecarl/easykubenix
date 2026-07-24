from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from ekn.sops import SopsDecryptError, maybe_decrypt


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def age_key(tmp_path: Path) -> Path:
    key_file = tmp_path / "key.txt"
    subprocess.run(["age-keygen", "-o", str(key_file)], check=True, capture_output=True)
    return key_file


def _public_key(key_file: Path) -> str:
    for line in key_file.read_text().splitlines():
        if line.startswith("# public key:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"no public key comment in {key_file}")


def _sops_encrypt(obj: dict, key_file: Path) -> dict:
    env = os.environ | {"SOPS_AGE_KEY_FILE": str(key_file)}
    proc = subprocess.run(
        [
            "sops",
            "--encrypt",
            "--input-type",
            "json",
            "--output-type",
            "json",
            "--age",
            _public_key(key_file),
            "/dev/stdin",
        ],
        input=json.dumps(obj).encode(),
        capture_output=True,
        env=env,
        check=True,
    )
    return json.loads(proc.stdout)


class TestMaybeDecrypt:
    async def test_passthrough_when_no_sops_key(self) -> None:
        obj = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "plain"}}

        result = await maybe_decrypt(obj)

        assert result == obj

    async def test_decrypts_a_sops_encrypted_object(self, age_key: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOPS_AGE_KEY_FILE", str(age_key))
        plain = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "test"},
            "data": {"password": "hunter2"},
        }
        encrypted = _sops_encrypt(plain, age_key)
        assert "sops" in encrypted

        result = await maybe_decrypt(encrypted)

        assert result == plain

    async def test_raises_on_decrypt_failure(
        self, age_key: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plain = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "test"}}
        encrypted = _sops_encrypt(plain, age_key)
        # Point at a key file with no matching identity -- decrypt must fail.
        wrong_key = tmp_path / "wrong.txt"
        await asyncio.to_thread(subprocess.run, ["age-keygen", "-o", str(wrong_key)], check=True, capture_output=True)
        monkeypatch.setenv("SOPS_AGE_KEY_FILE", str(wrong_key))

        with pytest.raises(SopsDecryptError):
            await maybe_decrypt(encrypted)
