{ lib, ... }:
{
  ekn.environment = "tofu-registry-fixture";

  deployment.units.registry = {
    class = "tf";
    path = "registry";
    modules = [
      (
        { tofuRegistry, lib, ... }:
        {
          # Two providers chosen to make the point the gate exists for.
          #
          # `keycloak/keycloak` is not in nixpkgs' `terraform-providers` at
          # all, so it can only come from the registry. `hashicorp/random` is,
          # which is what lets the gate show the registry choosing a version
          # independently of the nixpkgs pin.
          #
          # `latestWhere` rather than an exact string: a major version is where
          # a provider breaks compatibility, and pinning a patch means editing
          # this to take a fix.
          tofu.providers = _: [
            (tofuRegistry.keycloak.keycloak.latestWhere (v: lib.versionOlder v "6.0.0"))
            (tofuRegistry.hashicorp.random.latestWhere (v: lib.versionOlder v "4.0.0"))
          ];

          # Deliberately no `required_providers` here. tofu.nix derives it from
          # the providers above, and the gate checks that -- a hand-written one
          # is how a configuration asks for a version it did not pin.
          tofu.resource.random_pet.cluster.length = 2;
        }
      )
    ];
  };
}
