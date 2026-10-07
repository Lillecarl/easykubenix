"""ekn.tofuschema: the JSON Schema for a config.tf.json, and the check.

The core schema and provider dump here are small, but shaped exactly like
`ekn-tofuschema`'s export and `tofu providers schema -json`. Each verdict
below is what `tofu validate` (or `tofu plan`, where validate does not look)
answered for the same value with OpenTofu 1.12, except where a test says
the check is stricter.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from ekn.tofuschema import build_schema, check, provider_address

ANY = {"type": "dynamic"}


def _attr(constraint: dict[str, Any], *, required: bool = False, computed: bool = False) -> dict[str, Any]:
    return {"required": required, "optional": not required, "computed": computed, "constraint": constraint}


def _block(labels: list[str], body: dict[str, Any], nesting: str = "", **extra: Any) -> dict[str, Any]:
    return {"labels": labels, "nesting": nesting, "min_items": 0, "max_items": 0, "body": body, **extra}


_META = {
    "attributes": {
        "depends_on": _attr({"set": {"reference": True}, "min_items": 0, "max_items": 0}),
        "provider": _attr({"reference": True}),
    },
    "blocks": {
        "lifecycle": _block(
            [],
            {
                "attributes": {
                    "prevent_destroy": _attr({"type": "bool"}),
                    "ignore_changes": _attr(
                        {"one_of": [{"set": ANY, "min_items": 0, "max_items": 0}, {"keyword": "all"}]}
                    ),
                },
                "blocks": {},
            },
            "single",
        ),
    },
    "count": True,
    "for_each": True,
    "dynamic_blocks": True,
}

CORE: dict[str, Any] = {
    "attributes": {},
    "blocks": {
        "resource": _block(["type", "name"], _META),
        "data": _block(["type", "name"], _META),
        "provider": _block(["name"], {"attributes": {"alias": _attr({"type": "string"})}, "blocks": {}}),
        "variable": _block(["name"], {"attributes": {"type": _attr({"type_declaration": True})}, "blocks": {}}),
        "output": _block(["name"], {"attributes": {"value": _attr(ANY, required=True)}, "blocks": {}}),
        "locals": _block([], {"attributes": {}, "blocks": {}, "any_attribute": _attr(ANY)}),
        "module": _block(
            ["name"], {"attributes": {"source": _attr({"type": "string"}, required=True)}, "blocks": {}, "count": True}
        ),
        "terraform": _block(
            [],
            {
                "attributes": {"required_version": _attr({"type": "string"})},
                "blocks": {
                    "backend": _block(
                        ["type"],
                        {"attributes": {}, "blocks": {}},
                        dependent={"local": {"attributes": {"path": _attr({"type": "string"}, required=True)}}},
                    ),
                },
            },
        ),
        "check": _block(["name"], {"attributes": {}, "blocks": {"data": _block(["type", "name"], _META)}}),
    },
}

_SUBJECT = {"common_name": {"type": "string", "optional": True}}

DUMP: dict[str, Any] = {
    "format_version": "1.0",
    "provider_schemas": {
        "registry.opentofu.org/hashicorp/random": {
            "provider": {"block": {}},
            "resource_schemas": {
                "random_password": {
                    "block": {
                        "attributes": {
                            "length": {"type": "number", "required": True},
                            "lower": {"type": "bool", "optional": True, "computed": True},
                            "override_special": {"type": "string", "optional": True},
                            "keepers": {"type": ["map", "string"], "optional": True},
                            "bcrypt_hash": {"type": "string", "computed": True, "sensitive": True},
                        },
                    },
                },
                "random_pet": {"block": {"attributes": {"id": {"type": "string", "computed": True}}}},
            },
        },
        "registry.opentofu.org/hashicorp/tls": {
            "provider": {"block": {"attributes": {"proxy": {"type": "string", "optional": True}}}},
            "resource_schemas": {
                "tls_cert_request": {
                    "block": {
                        "attributes": {"private_key_pem": {"type": "string", "required": True}},
                        "block_types": {
                            "subject": {"nesting_mode": "list", "max_items": 1, "block": {"attributes": _SUBJECT}},
                            "extension": {
                                "nesting_mode": "list",
                                "min_items": 1,
                                "block": {"attributes": {"oid": {"type": "string", "required": True}}},
                            },
                        },
                    },
                },
                "tls_policy": {
                    "block": {
                        "attributes": {
                            "rules": {
                                "optional": True,
                                "nested_type": {
                                    "nesting_mode": "list",
                                    "attributes": {
                                        "expression": {"type": "string", "required": True},
                                        "message": {"type": "string", "optional": True},
                                    },
                                },
                            },
                            "pair": {"type": ["object", {"a": "string", "b": "number"}, ["b"]], "optional": True},
                        },
                    },
                },
            },
            "data_source_schemas": {
                "tls_certificate": {"block": {"attributes": {"url": {"type": "string", "optional": True}}}},
            },
        },
    },
}

REQUIRED = {"random": {"source": "hashicorp/random"}, "tls": {"source": "registry.opentofu.org/hashicorp/tls"}}


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return build_schema(CORE, DUMP, REQUIRED)


def _resource(kind: str, body: dict[str, Any]) -> dict[str, Any]:
    return {"resource": {kind: {"x": body}}}


def _messages(schema: dict[str, Any], config: dict[str, Any]) -> list[str]:
    return [str(violation) for violation in check(config, schema)]


def _password(**body: Any) -> dict[str, Any]:
    return _resource("random_password", {"length": 2, **body})


ACCEPTED = {
    "number": _password(),
    "numeric string": _resource("random_password", {"length": "2"}),
    "exponent string": _resource("random_password", {"length": "1e1"}),
    "template for a number": _resource("random_password", {"length": "${1+1}"}),
    "bool string": _password(lower="1"),
    "number for a string": _password(override_special=5),
    "bool for a string": _password(override_special=True),
    "map": _password(keepers={"a": 1}),
    "null": _password(keepers=None),
    "template for a map": _password(keepers="${{a = 1}}"),
    "comment": _password(**{"//": "a comment"}),
    "lifecycle": _password(lifecycle={"prevent_destroy": True, "ignore_changes": ["length"]}),
    "lifecycle all": _password(lifecycle={"ignore_changes": "all"}),
    "count": _password(count=2),
    "depends_on": _password(depends_on=["random_pet.a"]),
    "block as an object": _resource(
        "tls_cert_request", {"private_key_pem": "k", "subject": {"common_name": "a"}, "extension": {"oid": "1"}}
    ),
    "block as an array": _resource(
        "tls_cert_request", {"private_key_pem": "k", "subject": [{"common_name": "a"}], "extension": [{"oid": "1"}]}
    ),
    "dynamic block": _resource(
        "tls_cert_request",
        {"private_key_pem": "k", "dynamic": {"extension": {"for_each": "${[1]}", "content": {"oid": "1"}}}},
    ),
    "nested attribute": _resource("tls_policy", {"rules": [{"expression": "true"}]}),
    "object type with an optional member": _resource("tls_policy", {"pair": {"a": "x"}}),
    "provider": {"provider": {"tls": {"proxy": "x"}}},
    "provider aliases": {"provider": {"tls": [{"proxy": "x"}, {"alias": "b"}]}},
    "data source": {"data": {"tls_certificate": {"x": {"url": "u"}}}},
    "data in a check": {"check": {"c": {"data": {"tls_certificate": {"x": {"url": "u"}}}}}},
    "backend": {"terraform": {"backend": {"local": {"path": "x"}}}},
    "variable default": {"variable": {"v": {"type": "string", "default": {"any": ["thing"]}}}},
    "module inputs": {"module": {"m": {"source": "./m", "anything": 1}}},
    "locals": {"locals": {"a": 1, "b": {"c": [1]}}},
    "output": {"output": {"o": {"value": "${1}"}}},
}


@pytest.mark.parametrize("config", ACCEPTED.values(), ids=ACCEPTED.keys())
def test_accepted(schema: dict[str, Any], config: dict[str, Any]) -> None:
    assert _messages(schema, config) == []


REFUSED = {
    "word for a number": (
        _resource("random_password", {"length": "abc"}),
        "/resource/random_password/x/length: 'abc': expects a number or a ${...} template",
    ),
    "padded number": (
        _resource("random_password", {"length": " 2"}),
        "/resource/random_password/x/length: ' 2': expects a number or a ${...} template",
    ),
    "bool for a number": (
        _resource("random_password", {"length": True}),
        "/resource/random_password/x/length: True: expects a number or a ${...} template",
    ),
    "word for a bool": (
        _password(lower="yes"),
        "/resource/random_password/x/lower: 'yes': expects a bool or a ${...} template",
    ),
    "capitalised bool": (
        _password(lower="True"),
        "/resource/random_password/x/lower: 'True': expects a bool or a ${...} template",
    ),
    "number for a bool": (
        _password(lower=1),
        "/resource/random_password/x/lower: 1: expects a bool or a ${...} template",
    ),
    "word for a map": (
        _password(keepers="abc"),
        "/resource/random_password/x/keepers: 'abc': expects a map or a ${...} template",
    ),
    "list for a map": (
        _password(keepers=[1]),
        "/resource/random_password/x/keepers: [1]: expects a map or a ${...} template",
    ),
    "read-only attribute": (
        _password(bcrypt_hash="x"),
        "/resource/random_password/x/bcrypt_hash: 'x': read-only: the provider computes this attribute",
    ),
    "unknown attribute": (
        _password(nope=1),
        "/resource/random_password/x: Additional properties are not allowed ('nope' was unexpected)",
    ),
    "missing attribute": (
        _resource("random_password", {}),
        '/resource/random_password/x: "length" is a required property',
    ),
    "unknown lifecycle argument": (
        _password(lifecycle={"bogus": 1}),
        "/resource/random_password/x/lifecycle: Additional properties are not allowed ('bogus' was unexpected)",
    ),
    "word for count": (
        _password(count="x"),
        "/resource/random_password/x/count: 'x': expects a number or a ${...} template",
    ),
    "unknown resource type": (
        _resource("random_bogus", {}),
        "/resource/random_bogus: {'x': {}}: no provider in required_providers declares this resource type",
    ),
    "two of a one-block type": (
        _resource(
            "tls_cert_request",
            {
                "private_key_pem": "k",
                "extension": {"oid": "1"},
                "subject": [{"common_name": "a"}, {"common_name": "b"}],
            },
        ),
        '/resource/tls_cert_request/x/subject: [{"common_name":"a"},{"common_name":"b"}] has more than 1 item',
    ),
    "missing required block": (
        _resource("tls_cert_request", {"private_key_pem": "k"}),
        '/resource/tls_cert_request/x: "extension" is a required property',
    ),
    "dynamic block of an unknown type": (
        _resource(
            "tls_cert_request",
            {"private_key_pem": "k", "dynamic": {"nope": {"for_each": "${[1]}", "content": {}}}},
        ),
        "/resource/tls_cert_request/x/dynamic: Additional properties are not allowed ('nope' was unexpected)",
    ),
    # Stricter than tofu, which drops the key silently: it does nothing.
    "unknown key in a nested attribute": (
        _resource("tls_policy", {"rules": [{"expression": "true", "bogus": 1}]}),
        "/resource/tls_policy/x/rules/0: Additional properties are not allowed ('bogus' was unexpected)",
    ),
    "missing member of an object type": (
        _resource("tls_policy", {"pair": {"b": 1}}),
        '/resource/tls_policy/x/pair: "a" is a required property',
    ),
    # `tofu validate` does not read a provider block; `tofu plan` refuses it.
    "unknown provider argument": (
        {"provider": {"tls": {"bogus": 1}}},
        "/provider/tls: Additional properties are not allowed ('bogus' was unexpected)",
    ),
    "undeclared provider": (
        {"provider": {"aws": {"region": "x"}}},
        "/provider/aws: {'region': 'x'}: not a provider in required_providers",
    ),
    # `tofu validate` does not read a backend block; `tofu init` refuses it.
    "unknown backend argument": (
        {"terraform": {"backend": {"local": {"path": "x", "bogus": 1}}}},
        "/terraform/backend/local: Additional properties are not allowed ('bogus' was unexpected)",
    ),
    # ekn runs `tofu init` without `-backend-config`, so nothing else can
    # supply it; a backend argument read from the environment is refused too.
    "missing backend argument": (
        {"terraform": {"backend": {"local": {}}}},
        '/terraform/backend/local: "path" is a required property',
    ),
    "unknown backend": (
        {"terraform": {"backend": {"nope": {}}}},
        "/terraform/backend/nope: {}: unknown type for this block",
    ),
    "unknown data argument in a check": (
        {"check": {"c": {"data": {"tls_certificate": {"x": {"bogus": 1}}}}}},
        "/check/c/data/tls_certificate/x: Additional properties are not allowed ('bogus' was unexpected)",
    ),
    "unknown variable argument": (
        {"variable": {"v": {"bogus": 1}}},
        "/variable/v: Additional properties are not allowed ('bogus' was unexpected)",
    ),
}


@pytest.mark.parametrize(("config", "message"), REFUSED.values(), ids=REFUSED.keys())
def test_refused(schema: dict[str, Any], config: dict[str, Any], message: str) -> None:
    assert _messages(schema, config) == [message]


def test_a_value_failing_two_branches_is_one_violation(schema: dict[str, Any]) -> None:
    assert len(check(_resource("random_password", {"length": [2]}), schema)) == 1


def test_each_body_is_one_definition(schema: dict[str, Any]) -> None:
    """A body reached as itself and as dynamic content is emitted once."""
    subject_bodies = [
        name for name, body in schema["$defs"].items() if set(body.get("properties", {})) == {"//", "common_name"}
    ]
    assert len(subject_bodies) == 1


def test_the_prefix_owner_wins_a_shared_type() -> None:
    dump = {
        "provider_schemas": {
            "registry.opentofu.org/a/random": {
                "resource_schemas": {
                    "random_pet": {"block": {"attributes": {"other": {"type": "string", "optional": True}}}}
                }
            },
            **DUMP["provider_schemas"],
        }
    }
    schema = build_schema(CORE, dump, {**REQUIRED, "other": {"source": "a/random"}})
    assert check(_resource("random_pet", {}), schema) == []
    assert _messages(schema, _resource("random_pet", {"other": "x"})) == [
        "/resource/random_pet/x: Additional properties are not allowed ('other' was unexpected)"
    ]


def test_a_required_provider_missing_from_the_dump_is_an_error() -> None:
    with pytest.raises(
        ValueError, match=re.escape("registry.opentofu.org/hashicorp/aws is not in the provider schemas")
    ):
        build_schema(CORE, DUMP, {**REQUIRED, "aws": {"source": "hashicorp/aws"}})


@pytest.mark.parametrize(
    ("local", "source", "address"),
    [
        ("random", "hashicorp/random", "registry.opentofu.org/hashicorp/random"),
        ("random", None, "registry.opentofu.org/hashicorp/random"),
        ("x", "Example.com/Ns/X", "example.com/ns/x"),
    ],
)
def test_provider_address(local: str, source: str | None, address: str) -> None:
    assert provider_address(local, source) == address
