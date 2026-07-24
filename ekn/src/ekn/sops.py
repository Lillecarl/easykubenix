from __future__ import annotations

import asyncio
import base64
import json
import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import kr8s
import structlog
import yaml

from ekn.apply import apply_one, build_object

if TYPE_CHECKING:
    from kr8s._api import Api  # kr8s.asyncio.api() returns this, not kr8s.Api
    from nanopynix.models import JsonValue

    from ekn.apply import Manifest
    from ekn.eval import SopsAgeIdentity

_log = structlog.get_logger()


def _secret_manifest(name: str, namespace: str, *, string_data: dict[str, JsonValue] | None = None) -> Manifest:
    """Build a Secret manifest shape -- shared by `ensure_age_identities`'
    existence check (no `stringData`) and its actual apply (`stringData`
    set), so the two can't silently drift apart."""
    manifest: Manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": namespace},
    }
    if string_data is not None:
        manifest["stringData"] = string_data
    return manifest


class SopsDecryptError(RuntimeError):
    pass


class AgeKeygenError(RuntimeError):
    pass


class SopsUpdateKeysError(RuntimeError):
    pass


async def maybe_decrypt(obj: Manifest) -> Manifest:
    """Decrypt `obj` via the real `sops` CLI if it carries a `sops:`
    metadata block (the standard marker SOPS itself writes) -- otherwise
    return it unchanged.

    Shells out to `sops` rather than reimplementing its crypto/file format:
    ekn never encrypts anything itself, and SOPS-encrypted objects flow
    through the rest of the pipeline (`kubernetes.generated`, `ekn commit`'s
    git tree) untouched. This is the one place a direct apply (`ekn
    kubeapply`/`ekn validate` -- both bypass ArgoCD+kustomize+ksops
    entirely) needs to actually decrypt before the apiserver can use it.

    `--ignore-mac`: `obj` reached us via a Nix eval (`kubernetes.generated`
    -> JSON), and `builtins.toJSON` always alphabetizes attrset keys --
    every object here has a different key order than the on-disk file SOPS
    computed its whole-document MAC against, so that MAC would fail on
    every single object, not just tampered ones. Each field is still
    independently authenticated (its own AES-GCM tag from the `ENC[...]`
    value), so this only drops the additional whole-document consistency
    check that Nix's reordering makes unusable here -- it does not disable
    per-field decryption authentication.
    """
    if not isinstance(obj.get("sops"), dict):
        return obj

    proc = await asyncio.create_subprocess_exec(
        "sops",
        "--decrypt",
        "--ignore-mac",
        "--input-type",
        "json",
        "--output-type",
        "json",
        "/dev/stdin",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(json.dumps(obj).encode())
    if proc.returncode != 0:
        kind = obj.get("kind", "?")
        metadata_value = obj.get("metadata") or {}
        metadata: Manifest = metadata_value if isinstance(metadata_value, dict) else {}
        name = metadata.get("name", "?")
        raise SopsDecryptError(
            f"sops --decrypt failed for {kind}/{name}: {stderr.decode()}",
        )
    decrypted: JsonValue = json.loads(stdout)
    if not isinstance(decrypted, dict):
        raise SopsDecryptError(f"sops --decrypt returned non-object JSON: {stdout!r}")
    return decrypted


def _public_key_from_identity_text(key_text: str) -> str:
    return next(line.split(":", 1)[1].strip() for line in key_text.splitlines() if line.startswith("# public key:"))


async def _generate_age_identity() -> str:
    with tempfile.TemporaryDirectory() as tmp:
        key_file = Path(tmp) / "key.txt"
        proc = await asyncio.create_subprocess_exec(
            "age-keygen",
            "-o",
            str(key_file),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise AgeKeygenError(f"age-keygen failed: {stderr.decode()}")
        return key_file.read_text()


def _add_recipient_to_sops_config(
    config_file: str,
    sops_files: list[str],
    public_key: str,
) -> bool:
    """Add `public_key` as an age recipient to every `creation_rules` entry
    in `config_file` whose `path_regex` matches one of `sops_files` (matched
    relative to `config_file`'s own directory, mirroring how SOPS itself
    resolves path_regex). Idempotent -- a rule that already lists the key is
    left untouched. Returns whether anything actually changed, so the caller
    knows whether `sops updatekeys` needs to run at all.
    """
    path = Path(config_file)
    loaded: JsonValue = yaml.safe_load(path.read_text())
    config: Manifest = loaded if isinstance(loaded, dict) else {}
    rules_value: JsonValue = config.get("creation_rules") or []
    if not isinstance(rules_value, list):
        raise SopsUpdateKeysError(f"{config_file}: creation_rules is not a list")

    base_dir = path.parent
    relative_files = [str(Path(f).resolve().relative_to(base_dir.resolve())) for f in sops_files]

    changed = False
    for rule in rules_value:
        if not isinstance(rule, dict):
            raise SopsUpdateKeysError(f"{config_file}: creation_rules entry must be an attribute set")
        pattern = rule.get("path_regex")
        if not isinstance(pattern, str) or not pattern:
            continue
        if not any(re.search(pattern, f) for f in relative_files):
            continue
        age_value = rule.get("age")
        age_str = age_value if isinstance(age_value, str) else ""
        existing = [k.strip() for k in age_str.split(",") if k.strip()]
        if public_key in existing:
            continue
        rule["age"] = ",".join([*existing, public_key])
        changed = True

    if changed:
        path.write_text(yaml.safe_dump(config, sort_keys=False))
    return changed


async def _run_sops_updatekeys(config_file: str, sops_files: list[str]) -> None:
    for sops_file in sops_files:
        proc = await asyncio.create_subprocess_exec(
            "sops",
            "--config",
            config_file,
            "updatekeys",
            "--yes",
            sops_file,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise SopsUpdateKeysError(
                f"sops updatekeys failed for {sops_file}: {stderr.decode() or stdout.decode()}",
            )


async def ensure_age_identities(
    identities: list[SopsAgeIdentity],
    *,
    api: Api,
    field_manager: str = "ekn",
) -> None:
    """Idempotently ensure each declared SOPS age decrypt identity exists as
    a Secret, generating a fresh age keypair (via the real `age-keygen` CLI)
    the first time one is missing, and -- when the identity declares a
    `sopsConfigFile` -- registering its public key as a recipient there and
    re-running `sops updatekeys` on `sopsFiles` so their data key is
    rewrapped for it too.

    `identities` is `kubernetes.sopsAgeIdentities` -- any easykubenix module
    that needs a SOPS-decrypting workload (e.g. argocd.nix's ksops-enabled
    repo-server) declares its need there instead of a bespoke bootstrap
    script. This is the one place ekn actually generates key material and
    edits a git-tracked file; everywhere else it only ever decrypts what
    SOPS already produced (see `maybe_decrypt` above).

    Deliberately raises (aborting the whole `ekn kubeapply` run before it
    proceeds to actually apply anything) if either the `.sops.yaml` update
    or `sops updatekeys` fails, rather than leaving a half-configured setup
    where the identity Secret exists but nothing is encrypted for it yet.
    Re-running after such a failure is safe: the Secret is left as-is (an
    existing identity is never regenerated), and the config/updatekeys step
    is retried since it only checks -- never assumes -- that it already ran.
    """
    for identity in identities:
        namespace = identity.namespace
        secret_name = identity.secret_name
        key = identity.key

        secret = await build_object(_secret_manifest(secret_name, namespace), api)
        try:
            await secret.async_refresh()
            # kr8s.APIObject.raw returns a python-box `Box`, whose
            # __getitem__/get etc. ship with zero type annotations upstream
            # (box/box.py) -- pyright can't resolve indexing through it at
            # all, with or without an intervening annotated variable.
            key_text = base64.b64decode(secret.raw["data"][key]).decode()  # pyright: ignore[reportUnknownArgumentType] -- python-box Box's __getitem__ ships with zero upstream type annotations
        except kr8s.NotFoundError:
            # The Secret's own namespace may not exist yet either (e.g. a
            # fresh bootstrap target whose Namespace object hasn't been
            # applied in this same `ekn kubeapply` run) -- ensure it
            # directly rather than depending on apply ordering.
            await apply_one(
                {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}},
                api,
                field_manager=field_manager,
            )

            key_text = await _generate_age_identity()

            await apply_one(
                _secret_manifest(secret_name, namespace, string_data={key: key_text}),
                api,
                field_manager=field_manager,
            )
            _log.info("created SOPS age identity", namespace=namespace, secret=secret_name)

        public_key = _public_key_from_identity_text(key_text)

        sops_config_file = identity.sops_config_file
        if not sops_config_file:
            continue
        sops_files = identity.sops_files
        if _add_recipient_to_sops_config(sops_config_file, sops_files, public_key):
            await _run_sops_updatekeys(sops_config_file, sops_files)
            _log.info(
                "registered SOPS age recipient and ran sops updatekeys",
                namespace=namespace,
                secret=secret_name,
                public_key=public_key,
                config_file=sops_config_file,
                sops_files=sops_files,
            )


__all__ = [
    "AgeKeygenError",
    "SopsDecryptError",
    "SopsUpdateKeysError",
    "ensure_age_identities",
    "maybe_decrypt",
]
