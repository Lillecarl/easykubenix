"""`tofu validate` against `ekn _tofuSchemaCheck`, case by case.

Usage: differential.py TOFU EKN SCHEMA BASE

BASE is a config.tf.json holding the fixture's `terraform` block. Each case
is merged over it, written to the working directory, and given to both
tools. A case passes when both agree, or when it is one of the differences
`STRICTER` names. Run in a directory where `tofu init -backend=false` has
already run.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


def password(**body: Any) -> dict[str, Any]:
    return {"resource": {"random_password": {"x": {"length": 2, **body}}}}


# name -> (config, tofu validate's verdict)
CASES: dict[str, tuple[dict[str, Any], bool]] = {
    "number": (password(), True),
    "numeric string": (password(length="2"), True),
    "exponent string": (password(length="1e1"), True),
    "word for a number": (password(length="abc"), False),
    "padded number": (password(length=" 2"), False),
    "hex number": (password(length="0x10"), False),
    "bool for a number": (password(length=True), False),
    "list for a number": (password(length=[2]), False),
    "template for a number": (password(length="${1+1}"), True),
    "word for a bool": (password(lower="yes"), False),
    "capitalised bool": (password(lower="True"), False),
    "one for a bool": (password(lower="1"), True),
    "number for a bool": (password(lower=1), False),
    "number for a string": (password(override_special=5), True),
    "bool for a string": (password(override_special=True), True),
    "object for a string": (password(override_special={"a": 1}), False),
    "map": (password(keepers={"a": 1}), True),
    "word for a map": (password(keepers="abc"), False),
    "nested map": (password(keepers={"a": {"b": 1}}), False),
    "list for a map": (password(keepers=[1]), False),
    "null": (password(keepers=None), True),
    "read-only attribute": (password(bcrypt_hash="x"), False),
    "unknown attribute": (password(nope=1), False),
    "missing attribute": ({"resource": {"random_password": {"x": {}}}}, False),
    "comment": (password(**{"//": "a comment"}), True),
    "lifecycle": (password(lifecycle={"prevent_destroy": True, "ignore_changes": ["length"]}), True),
    "lifecycle all": (password(lifecycle={"ignore_changes": "all"}), True),
    "unknown lifecycle argument": (password(lifecycle={"bogus": 1}), False),
    "count": (password(count=2), True),
    "word for count": (password(count="x"), False),
    "depends_on": ({"resource": {"random_pet": {"a": {}}, **password(depends_on=["random_pet.a"])["resource"]}}, True),
    "unknown resource type": ({"resource": {"random_bogus": {"x": {}}}}, False),
    "provider": ({"provider": {"random": {}}}, True),
    "undeclared provider": ({"provider": {"aws": {"region": "x"}}}, False),
    "unknown provider argument": ({"provider": {"random": {"bogus": 1}}}, True),
    "unknown backend argument": ({"terraform": {"backend": {"local": {"bogus": 1}}}}, True),
    "variable": ({"variable": {"v": {"type": "string", "default": "x"}}}, True),
    "unknown variable argument": ({"variable": {"v": {"bogus": 1}}}, False),
    "output": ({"output": {"o": {"value": "${1}", "sensitive": True}}}, True),
    "unknown output argument": ({"output": {"o": {"value": 1, "bogus": 1}}}, False),
    "locals": ({"locals": {"a": 1, "b": {"c": [1]}}}, True),
}

#: Cases ekn refuses and `tofu validate` accepts, and why that is right.
STRICTER = {
    "unknown provider argument": "validate does not read a provider block; `tofu plan` refuses it",
    "unknown backend argument": "validate does not read the backend; `tofu init` refuses it",
}


def merged(base: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in case.items():
        if key == "terraform":
            out["terraform"] = {**out["terraform"], **value}
        else:
            out[key] = value
    return out


def main() -> int:
    tofu, ekn, schema, base_file = sys.argv[1:5]
    base = json.loads(Path(base_file).read_text())
    failed = 0
    for name, (case, expected) in CASES.items():
        Path("config.tf.json").write_text(json.dumps(merged(base, case)))
        validate = subprocess.run([tofu, "validate", "-json", "-no-color"], capture_output=True, text=True, check=False)
        tofu_ok = json.loads(validate.stdout)["valid"]
        ekn_run = subprocess.run(
            [ekn, "_tofuSchemaCheck", "config.tf.json", "--schema", schema], capture_output=True, text=True, check=False
        )
        ekn_ok = ekn_run.returncode == 0
        want_ekn = expected and name not in STRICTER
        verdict = "ok" if (tofu_ok, ekn_ok) == (expected, want_ekn) else "FAIL"
        failed += verdict == "FAIL"
        note = f" ({STRICTER[name]})" if name in STRICTER else ""
        print(f"{verdict} {name}: tofu={tofu_ok} ekn={ekn_ok}{note}")
        if verdict == "FAIL":
            print(validate.stdout, ekn_run.stdout, ekn_run.stderr, sep="\n")
    print(f"{len(CASES) - failed} of {len(CASES)} cases agree")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
