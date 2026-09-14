# A `tf` unit whose providers come from the OpenTofu registry rather than from
# nixpkgs.
#
#     nix build --file ./checks.nix tofu-registry
#
# Deliberately outside `checks.all`, for the same reason as `kubeapply`: it
# fetches the registry index, which is a ~424M tree, plus a provider zip per
# entry. That is minutes on a cold CI runner for a path the cheaper
# `tofu-render` gate already covers structurally. Run it when you touch
# `lib/tofuRegistry.nix` or the provider plumbing.
#
# What only this can answer: whether a provider nixpkgs does not package
# resolves from the store with no network. `tofu init` inside the build sandbox
# is that question -- there is nothing to fall back to there, so an install
# line for `keycloak/keycloak` is proof the pin reached the plugin directory.
{
  pkgs,
  lib,
  sources,
}:
let
  eval = import ../../default.nix {
    inherit pkgs sources;
    modules = [ ./registry-fixture.nix ];
  };

  unit = eval.config.deployment.tofuUnits.registry;
in
pkgs.runCommand "easykubenix-check-tofu-registry"
  {
    HOME = "/build/home";
    CHECKPOINT_DISABLE = "1";
    TF_IN_AUTOMATION = "1";
  }
  ''
    mkdir -p "$HOME" work
    cd work
    cp ${unit.configFile}/config.tf.json .

    # `required_providers` is derived from `tofu.providers`, and the fixture
    # writes none. Both entries present means the derivation fired; the source
    # host being registry.opentofu.org is what makes them resolvable from the
    # plugin tree at all.
    ${pkgs.jq}/bin/jq -e '
      .terraform.required_providers
      | (keys == ["keycloak", "random", "talos"])
        and (.keycloak.source == "registry.opentofu.org/keycloak/keycloak")
        and (.random.source == "registry.opentofu.org/hashicorp/random")
        and (.talos.source == "registry.opentofu.org/siderolabs/talos")
    ' config.tf.json > /dev/null

    # No selected version is a prerelease. A version bound does not exclude
    # them and the index carries them, so `latestWhere (v: versionOlder v
    # "1.0.0")` over siderolabs/talos used to pick 0.12.0-beta.0 and say
    # nothing about it -- a beta provider reaching real state, visible only as
    # a version string in a file nobody has to read.
    ${pkgs.jq}/bin/jq -e 'all(.[]; .version | contains("-") | not)' \
      ${unit.configFile}/providers.json > /dev/null

    # The versions are read back rather than pinned to literals, which would
    # only test the registry lock. What is gated is that a version was chosen
    # and recorded, and that it honours the fixture's `latestWhere` bound.
    ${pkgs.jq}/bin/jq -e '
      (.[] | select(.name == "terraform-provider-keycloak-keycloak").version | test("^5\\."))
      and (.[] | select(.name == "terraform-provider-hashicorp-random").version | test("^3\\."))
    ' ${unit.configFile}/providers.json > /dev/null

    # The claim. No network in here, so anything not already in the store
    # cannot be installed.
    ${unit.tofu} init -input=false
    ${unit.tofu} validate

    touch "$out"
  ''
