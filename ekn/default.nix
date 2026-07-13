{
  lib,
  buildPythonApplication,
  cacert,
  installShellFiles,
  hatchling,
  nanopynix,
  pygit2,
  pyyaml,
  clypi,
  anyio,
  structlog,
  rich,
}:
buildPythonApplication (finalAttrs: {
  name = "ekn";
  version = (fromTOML (builtins.readFile ./pyproject.toml)).project.version;
  pyproject = true;

  src = lib.cleanSource ./.;

  build-system = [ hatchling ];
  nativeBuildInputs = [
    cacert
    installShellFiles
  ];
  dependencies = [
    nanopynix
    pygit2
    pyyaml
    clypi
    anyio
    structlog
    rich
  ];

  postInstall = ''
    export GIT_SSL_CAINFO="${cacert}/etc/ssl/certs/ca-bundle.crt"
    installShellCompletion --name ekn --bash <(env _EKN_COMPLETE=source_bash $out/bin/ekn)
    installShellCompletion --name ekn --zsh  <(env _EKN_COMPLETE=source_zsh  $out/bin/ekn)
    installShellCompletion --name ekn --fish <(env _EKN_COMPLETE=source_fish $out/bin/ekn)
  '';

  meta = {
    mainProgram = finalAttrs.name;
    description = "easykubenix CLI";
    maintainers = [ lib.maintainers.lillecarl ];
    homepage = "https://github.com/lillecarl/easykubenix";
  };
})
