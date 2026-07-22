# Parse a multi-document YAML stream into a filtered (no null/empty
# documents) list of JSON-ish values.
#
# Prefers nanopynix's fromYAML11Stream/fromYAMLStream primops (registered
# per-Session via `yaml_primops()`, present only when evaluated through
# `ekn`'s worker) to parse in-process. Falls back to shelling out to ekn's
# own hidden `_yamlToJson` CLI subcommand (plain `nix build`/`nix eval`, no
# nanopynix primops registered) -- the exact same nanopynix YAML-parsing
# code, just out-of-process, so both paths agree on `yamlVersion` scalar
# semantics regardless of which one a given evaluation takes.
{
  lib,
  pkgs,
  eknPackage,
}:
{
  # Derivation/store path (or any value accepted by string interpolation)
  # containing the YAML document stream to parse.
  src,
  # "yaml11" | "yaml12" -- selects fromYAML11Stream/`--yaml-version yaml11`
  # (bare leading-zero numbers resolve as octal, e.g. a volume's
  # `defaultMode: 0644` means 420) vs fromYAMLStream/`--yaml-version
  # yaml12` (the same literal reads as decimal 644).
  yamlVersion,
}:
let
  streamBuiltins = {
    yaml11 = "fromYAML11Stream";
    yaml12 = "fromYAMLStream";
  };
  streamBuiltin = streamBuiltins.${yamlVersion};
in
lib.filter (object: object != null) (
  if builtins ? ${streamBuiltin} then
    builtins.${streamBuiltin} (builtins.readFile src)
  else
    lib.importJSON (
      pkgs.runCommand "yaml2json" { nativeBuildInputs = [ eknPackage ]; } # bash
        ''
          ekn _yamlToJson --yaml-version ${yamlVersion} < ${src} >$out
        ''
    )
)
