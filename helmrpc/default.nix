{
  lib,
  buildGoModule,
}:
buildGoModule (finalAttrs: {
  pname = "helmrpc";
  version = "0.1.0";

  src = lib.cleanSource ./.;

  vendorHash = "sha256-q+B0TA9BJrvZWvkB22RbpYoqcioUR+lLPQcK+rtbwW4=";

  meta = {
    mainProgram = finalAttrs.pname;
    description = "Helm served over a stdio-transported gRPC connection, wire-compatible with grpclib-transports' StdioChannel";
    maintainers = [ lib.maintainers.lillecarl ];
    homepage = "https://github.com/lillecarl/easykubenix";
  };
})
