# Parse a multi-document YAML stream into a filtered (no null/empty
# documents) list of JSON-ish values.
#
# **One reader, and it is go-yaml.** `ekn-yaml2json` (tools/yaml2json) reads
# the stream through `sigs.k8s.io/yaml`, which is the parser Helm renders
# through and the API server decodes with. Kubernetes YAML is that parser and
# not a YAML version: `defaultMode: 0644` is 420, which YAML 1.2 reads as 644,
# and `value: 1e+06` is a number, which YAML 1.1 reads as a string.
#
# nanopynix's `fromYAML11Stream` primop described the same dialect in PyYAML.
# It parsed in-process, which is faster, and it was wrong in six classes -- an
# unquoted `1:30` came back as 90, an unquoted `n` as the string "n" where
# Kubernetes reads false, and an unquoted date stopped the whole stream.
# tools/yaml2json/fuzz.py found them by generating YAML for both readers, and
# nanopynix #307 carries the table. A parser that has to be described is a
# parser that drifts from its description; this one is inherited.
#
# The cost is a derivation per stream rather than an in-process call. The
# store caches it, and the conversion itself is 16 ms against the 1431 ms the
# Python reader spent starting up.
{
  lib,
  pkgs,
  yaml2json,
}:
{
  # Derivation, store path, or any value string interpolation accepts,
  # holding the YAML document stream.
  src,
}:
lib.importJSON (
  pkgs.runCommand "yaml2json" { nativeBuildInputs = [ yaml2json ]; } # bash
    ''
      ekn-yaml2json --shape list < ${src} > $out
    ''
)
