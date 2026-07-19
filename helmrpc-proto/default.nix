{
  lib,
  buildPythonPackage,
  hatchling,
  # python protobuf runtime library (google.protobuf), a real runtime
  # dependency of the generated code.
  protobuf,
  # the protoc compiler CLI; must be passed explicitly as pkgs.protobuf,
  # since python3Packages.protobuf (the default resolution of `protobuf`
  # here) is the runtime library above and has no `bin/protoc`.
  protoc,
  grpclib,
  python,
}:
buildPythonPackage (finalAttrs: {
  pname = "helmrpc-proto";
  version = (fromTOML (builtins.readFile ./pyproject.toml)).project.version;
  pyproject = true;

  src = lib.cleanSource ./.;

  build-system = [ hatchling ];
  nativeBuildInputs = [
    protoc
  ];
  dependencies = [
    grpclib
    protobuf
  ];

  # helmrpc.proto lives in ../helmrpc alongside the Go service it describes,
  # so both sides regenerate from the same source of truth instead of
  # drifting out of sync.
  preBuild = ''
    mkdir -p src/helmrpc_proto proto
    cp ${../helmrpc/proto/helmrpc.proto} proto/helmrpc.proto
    touch src/helmrpc_proto/__init__.py
    touch src/helmrpc_proto/py.typed
    # grpclib's own protoc-gen-python_grpc wrapper doesn't propagate the
    # python protobuf runtime onto its PYTHONPATH (protobuf is only needed
    # by the plugin itself, not grpclib's core), so its plugin/main.py can't
    # import google.protobuf on its own; supply it here instead of building
    # a whole extra interpreter just to combine the two.
    PYTHONPATH="${protobuf}/${python.sitePackages}" \
      protoc \
        --proto_path=proto \
        --python_out=src/helmrpc_proto \
        --pyi_out=src/helmrpc_proto \
        --plugin=protoc-gen-python_grpc=${lib.getExe' grpclib "protoc-gen-python_grpc"} \
        --python_grpc_out=src/helmrpc_proto \
        proto/helmrpc.proto
    # grpclib's protoc plugin emits a bare top-level "import helmrpc_pb2",
    # which only resolves when the generated module sits unpackaged on
    # sys.path. Rewrite it to a package-relative import so helmrpc_proto
    # works as a normal installed package.
    sed -i 's/^import helmrpc_pb2$/from . import helmrpc_pb2/' src/helmrpc_proto/helmrpc_grpc.py
  '';

  pythonImportsCheck = [
    "helmrpc_proto.helmrpc_pb2"
    "helmrpc_proto.helmrpc_grpc"
  ];

  meta = {
    description = "Generated Python protobuf/grpclib bindings for helmrpc";
    maintainers = [ lib.maintainers.lillecarl ];
    homepage = "https://github.com/lillecarl/easykubenix";
  };
})
