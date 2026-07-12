{
  lib,
  buildPythonApplication,
  cacert,
  hatchling,
  installShellFiles,
  nanopynix,
  pygit2,
  pyyaml,
  typer,
}:
buildPythonApplication (finalAttrs: {
  name = "ekn";
  version = (fromTOML (builtins.readFile ./pyproject.toml)).project.version;
  pyproject = true;

  src = lib.cleanSource ./.;

  build-system = [ hatchling ];
  nativeBuildInputs = [ installShellFiles cacert ];
  dependencies = [
    nanopynix
    pygit2
    pyyaml
    typer
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
