# `tofu.schemaCheck` against `tofu validate`.
#
#     nix build --file ./checks.nix tofu-schema
#
# `ekn.tofuschema` turns OpenTofu's core schema and the providers' schemas
# into a JSON Schema, and its rules for HCL's JSON syntax were measured
# against `tofu validate`. This gate holds them to it: every case in
# ./differential.py goes to both, and they must agree. A disagreement fails
# the build unless the script names it as one where ekn is right to be
# stricter. The pinned OpenTofu moves with nixpkgs, so a rule it changes
# fails here first.
#
# `tofu validate` is the oracle and nothing more. No deploy path runs it.
{
  pkgs,
  lib,
  sources,
}:
let
  eval = import ../../default.nix {
    inherit pkgs sources;
    modules = [ ./fixture.nix ];
  };

  instance = eval.config.deployment.units.infra.instance.config;
  unit = eval.config.deployment.tofuUnits.infra;
in
pkgs.runCommand "easykubenix-check-tofu-schema"
  {
    nativeBuildInputs = [
      pkgs.jq
      pkgs.python3
    ];
  }
  ''
    export HOME="$TMPDIR/home" CHECKPOINT_DISABLE=1 TF_IN_AUTOMATION=1
    mkdir -p "$HOME" work
    cd work

    # The rendered fixture passes; the build of `tofu.schemaCheck` says so.
    test -e ${instance.tofu.schemaCheck}

    jq '{terraform}' ${unit.configFile}/config.tf.json > base.json
    cp base.json config.tf.json
    ${unit.tofu} init -backend=false -input=false > /dev/null
    python3 ${./differential.py} ${unit.tofu} ${lib.getExe' eval.passthru.ekn "ekn"} \
      ${unit.jsonSchema} base.json
    touch "$out"
  ''
