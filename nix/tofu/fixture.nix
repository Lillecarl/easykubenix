{ ekn, ... }:
{
  # `ekn.environment` is required of every Kubernetes instance and has no
  # default. The unit below is the subject; this satisfies the instance holding
  # it, which renders no object of its own.
  ekn.environment = "tofu-render-fixture";

  deployment.units.infra = {
    class = "tf";
    path = "infra";
    modules = [
      (
        { ... }:
        {
          # `random` and nothing else. It is the provider nixpkgs' own
          # opentofu plugin test uses, it has no credentials and no API behind
          # it, and `tofu validate` type-checks a `random_pet` against its real
          # schema exactly as it would an `aws_instance`.
          tofu.providers = plugins: [ plugins.hashicorp_random ];

          tofu.terraform = {
            required_providers.random.source = "hashicorp/random";
            # Relative, so it lands in the build directory. A `tf` unit in
            # earnest names a real backend; `tofu validate` needs a backend
            # block to parse, not a reachable one.
            backend.local.path = "terraform.tfstate";
          };

          tofu.resource.random_pet.cluster = {
            length = 2;
            separator = "-";
            # Dropped before render. OpenTofu reads an explicit null as a set
            # value rather than an absent one, and the module system produces
            # nulls freely.
            keepers = null;
          };

          # The two interpolation directions, which is the trap worth gating.
          # `ref` writes an expression on purpose; `escape` says a string
          # holding `${` is literal text. Getting the second wrong fails inside
          # `tofu` with a message about an unknown variable, naming neither the
          # option nor the Nix string behind it.
          tofu.output.cluster_name.value = ekn.lib.tf.ref "random_pet.cluster.id";
          tofu.output.literal.value = ekn.lib.tf.escape "not \${HOME} but a literal";
        }
      )
    ];
  };
}
