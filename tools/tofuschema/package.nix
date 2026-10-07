{
  buildGoModule,
}:
buildGoModule {
  pname = "ekn-tofuschema";
  version = "0-unstable";
  src = ./.;
  vendorHash = "sha256-kaokSu+YJ0/n8Dkxuq2eOB/mEMZnycR2KHlCRzx/1dQ=";
  # buildGoModule names the binary after the go.mod `module` directive's last
  # component, not after `pname`.
  meta.mainProgram = "ekn-tofuschema";
}
