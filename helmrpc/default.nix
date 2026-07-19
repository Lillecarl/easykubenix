{
  lib,
  buildGoModule,
}:
buildGoModule (finalAttrs: {
  pname = "helmrpc";
  version = "0.1.0";

  src = lib.cleanSource ./.;

  vendorHash = "sha256-K4t9ts4unj19zmuSBSJXUs892UWpfgDBnM1EIpXMtcY=";

  meta = {
    mainProgram = finalAttrs.pname;
    description = "Helm served over a stdio-transported gRPC connection, wire-compatible with grpclib-transports' StdioChannel";
    maintainers = [ lib.maintainers.lillecarl ];
    homepage = "https://github.com/lillecarl/easykubenix";
  };
})
