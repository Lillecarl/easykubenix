{
  buildGoModule,
}:
buildGoModule {
  pname = "ekn-yaml2json";
  version = "0-unstable";
  src = ./.;
  vendorHash = "sha256-HmRB7qs2rgpP6ovqlqFvIROg/SoNNHfsofj4dPXpjAs=";
  # buildGoModule names the binary after the go.mod `module` directive's last
  # component, not after `pname`.
  meta.mainProgram = "ekn-yaml2json";
}
