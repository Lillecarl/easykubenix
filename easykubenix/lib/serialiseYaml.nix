# Serialise a JSON-ish Nix value to YAML text.
#
# Prefers nanopynix's `toYAML` primop (registered per-Session via the same
# mechanism as the yaml primops, present only when evaluated through `ekn`'s
# worker) to serialise in-process. Falls back to shelling out to ekn's own
# hidden `_jsonToYAML` CLI subcommand (plain `nix build`/`nix eval`, no
# nanopynix primops registered) -- which reuses nanopynix's `to_yaml`, the
# exact same code the primop calls, just out-of-process, so both paths are
# byte-for-byte identical.
#
# The write-direction mirror of lib/parseYamlStream.nix. Without it, any
# consumer calling `builtins.toYAML` directly cannot be evaluated by stock Nix
# *at all* -- evaluation dies with `attribute 'toYAML' missing`, not merely
# slower or less accurate.
#
# Drop-in for `builtins.toYAML`: takes the value positionally and returns a
# string, so call sites only change the prefix.
#
# Caveat: on the fallback path the value round-trips through
# `builtins.toJSON`, so it must be JSON-representable, and any string context
# is materialised into the derivation's input file rather than carried through
# to the returned string. The primop path has no such round trip. Values here
# are ordinary config data (see the opensearch securityconfig Secrets), so
# this has no practical effect, but it is a real difference between the paths.
{
  lib,
  pkgs,
  eknPackage,
}:
value:
if builtins ? toYAML then
  builtins.toYAML value
else
  builtins.readFile (
    pkgs.runCommand "toYAML"
      {
        nativeBuildInputs = [ eknPackage ];
        json = builtins.toJSON value;
        # Keep large documents out of the process environment.
        passAsFile = [ "json" ];
      } # bash
      ''
        ekn _jsonToYAML < "$jsonPath" > $out
      ''
  )
