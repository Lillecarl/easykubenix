# A `class = "tf"` deployment unit, rendered and then handed to `tofu`.
#
#     nix build --file ./checks.nix tofu-render
#
# Two questions, and they need different answers to be useful.
#
# The first is what the module system produced. The fixture below sets a null
# on a resource argument, leaves most of the option tree empty, and writes one
# string of each interpolation kind -- so the expected JSON pins null-dropping,
# empty-block omission and both directions of the `${...}` rule at once. A
# `diff` against a literal is the check: an option-level assertion would test
# the module system, not the file that ships.
#
# The second is whether `tofu` agrees. `tofu validate` parses the configuration
# and type-checks every resource against the provider's real schema, which is
# the half Nix cannot know -- a misspelled argument is a valid JSON document.
#
# It runs in the build sandbox, which has no network. That is not incidental:
# it is what proves `tofu.providers` pins the provider through the store rather
# than leaving `tofu init` to reach the registry. nixpkgs makes the same point
# about its own opentofu wrapper by pointing the proxy at a dead port; here the
# sandbox does it for free.
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

  unit = eval.config.deployment.tofuUnits.infra;

  expected = builtins.toJSON {
    output = {
      cluster_name.value = "\${random_pet.cluster.id}";
      literal.value = "not $\${HOME} but a literal";
    };
    resource.random_pet.cluster = {
      length = 2;
      separator = "-";
    };
    terraform = {
      backend.local.path = "terraform.tfstate";
      required_providers.random.source = "hashicorp/random";
    };
  };
in
pkgs.runCommand "easykubenix-check-tofu-render"
  {
    # `tofu` writes its plugin cache and its CLI configuration under `$HOME`,
    # and the builder's default (`/homeless-shelter`) is read-only.
    HOME = "/build/home";

    # No version check against the registry, and no interactive prompt on a
    # terminal that is not one.
    CHECKPOINT_DISABLE = "1";
    TF_IN_AUTOMATION = "1";

    inherit expected;
    passAsFile = [ "expected" ];
  }
  ''
    mkdir -p "$HOME" work
    cd work

    cp ${unit.configFile}/config.tf.json .

    # Both sides through `jq -S`, so the comparison is of the data and not of
    # whitespace or key order. `config.tf.json` is already pretty-printed and
    # sorted (see tofu.nix); `expected` comes straight from `builtins.toJSON`.
    ${pkgs.jq}/bin/jq -S . "$expectedPath" > want.json
    ${pkgs.jq}/bin/jq -S . config.tf.json > got.json
    diff -u want.json got.json

    ${unit.tofu} init -input=false
    ${unit.tofu} validate

    touch "$out"
  ''
