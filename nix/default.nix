{
  inputs ? (import ./compat.nix).inputs,
  system ? builtins.currentSystem,
  pkgs ? inputs.nixpkgs.legacyPackages.${system},
}:
let
  inherit (pkgs) lib;

  # `ekn`'s source lives in ../ekn, but its dependency closure comes from
  # nanopynix' exported development environment. See shell.nix.
  nanopynix = import inputs.nanopynix { inherit pkgs; };

  root = import ../default.nix { inherit pkgs; };

  # The gates over the doc examples. Plain derivations, so they build from a
  # bare `import` and do not need `nix flake check` -- see ../checks.nix for
  # the entry point CI uses, and ../docs/examples/default.nix for what they
  # are. `flake.nix` re-exports these; it is not where they live.
  examples = import ../docs/examples { inherit inputs system pkgs; };

  # `ekn` runs inside a Nix build sandbox from three derivations in this
  # repository -- `_yamlToJson` (lib/parseYamlStream.nix, pkgs/renderChart.nix),
  # `_jsonToYAML` and `split-manifest` (both easykubenix/internal.nix) -- and a
  # sandbox has no ambient trust store. `ekn` imports pygit2, which initialises
  # OpenSSL at import and refuses to start without one, so until the CA bundle
  # moved onto the program itself it could not run here at all (nanopynix issue
  # #62, fixed by `mkApp`'s `caBundle` wrapper). This derivation is that
  # sandbox, so the crash cannot come back unseen.
  #
  # The gate belongs here and not in nanopynix, even though the wrapper it
  # guards is nanopynix': every call site that runs `ekn` without certificates
  # is in this repository. A test under ../tests could not stand in for it,
  # because the `ekn` on the dev shell's PATH is the venv's own console script,
  # not the wrapped application.
  #
  # The YAML 1.1 case is deliberate. `0644` reads as octal 420 there, which is
  # the scalar semantics this out-of-process fallback exists to keep.
  ekn-sandbox =
    pkgs.runCommand "easykubenix-check-ekn-sandbox"
      {
        nativeBuildInputs = [ root.passthru.ekn ];
      }
      ''
        printf 'a: 1\n---\nb: 0644\n' | ekn _yamlToJson --yaml-version yaml11 >got.json
        printf '%s' '[{"a":1},{"b":420}]' >want.json
        diff -u want.json got.json

        printf '%s' '{"a":1,"b":"x"}' | ekn _jsonToYAML >got.yaml
        printf 'a: 1\nb: x\n' >want.yaml
        diff -u want.yaml got.yaml

        touch "$out"
      '';

  # **The completion scripts the package installs, and the answer they get.**
  #
  # Two questions, and one derivation answers both. The first is whether the
  # three files are there at all: `mkApp` renders them only when it is asked to
  # (`completions = true` in ../default.nix), and a package that quietly stops
  # installing them reports nothing -- the shell simply offers file names
  # again. The second is whether the program still speaks the protocol behind
  # them, which is what the scripts assume and cannot check.
  #
  # The protocol is the whole of the second command below. `_ARGCOMPLETE` says
  # this run is a completion, `COMP_LINE` and `COMP_POINT` carry the line and
  # the cursor, and the answer comes back on file descriptor 8 -- so stdout can
  # hold anything and a completion still works. `_ARGCOMPLETE_IFS` separates
  # the candidates, and one space is enough for a line with one answer.
  #
  # `tests/test_cli_completion.py` covers what `ekn` offers for many more
  # lines, in-process. It cannot cover this: the `ekn` on the dev shell's PATH
  # is the venv's own console script, not the installed application, and the
  # `share/` tree only exists here.
  ekn-completions =
    pkgs.runCommand "easykubenix-check-ekn-completions"
      {
        ekn = root.passthru.ekn;
      }
      ''
        for file in \
          share/bash-completion/completions/ekn.bash \
          share/zsh/site-functions/_ekn \
          share/fish/vendor_completions.d/ekn.fish
        do
          if [ ! -s "$ekn/$file" ]; then
            echo "the package installs no $file"
            exit 1
          fi
          grep -q _ARGCOMPLETE "$ekn/$file" || { echo "$file names no _ARGCOMPLETE"; exit 1; }
          grep -q '8>&1' "$ekn/$file" || { echo "$file reads no file descriptor 8"; exit 1; }
        done

        got=$(
          _ARGCOMPLETE=1 _ARGCOMPLETE_IFS=' ' _ARGCOMPLETE_SHELL=bash \
          COMP_LINE='ekn depl' COMP_POINT=8 COMP_TYPE=9 \
          "$ekn/bin/ekn" 8>&1 9>/dev/null 1>/dev/null 2>/dev/null
        )
        if [ "$got" != 'deploy ' ]; then
          echo "a completion of 'ekn depl' answered '$got', not 'deploy '"
          exit 1
        fi

        touch "$out"
      '';

  # Every `.nix` file in the repository, and nothing else. The filter names
  # what to *drop* rather than what to keep, so a directory added later is
  # covered without anyone remembering to list it -- a formatting gate that
  # silently stops seeing new files is worse than no gate. Dot-directories go
  # (`.git`, `.direnv`, `.pytest-agent`, `.claude`), as do `result` symlinks
  # and `__pycache__`; none of them holds source this repository owns, and
  # `.git` in particular must never be walked.
  nixSources = builtins.path {
    name = "easykubenix-nix-sources";
    path = ../.;
    filter =
      path: type:
      let
        base = baseNameOf path;
      in
      if type == "directory" then
        !(
          lib.hasPrefix "." base || base == "result" || lib.hasPrefix "result-" base || base == "__pycache__"
        )
      else
        lib.hasSuffix ".nix" base;
  };

  # nixfmt is the formatter, at the version this repository's nixpkgs pin
  # carries -- which is the point of running it from a derivation rather than
  # from whatever happens to be on a contributor's PATH. `--check` writes
  # nothing, so the read-only store copy above is all it needs.
  nixfmt = pkgs.runCommand "easykubenix-check-nixfmt" { nativeBuildInputs = [ pkgs.nixfmt ]; } ''
    cd ${nixSources}
    find . -type f -name '*.nix' -print0 | sort -z | xargs -0 nixfmt --check
    touch "$out"
  '';
in
{
  inherit
    root
    ekn-sandbox
    ekn-completions
    nixfmt
    ;
  inherit (examples) packages;

  shell = pkgs.python3Packages.callPackage ./shell.nix {
    inherit (root.passthru) eknDevEnv;
  };

  checks = examples.checks // {
    inherit ekn-sandbox ekn-completions nixfmt;
    # `all` is what CI builds, so a gate that is not in it is a gate that does
    # not run. ../docs/examples/default.nix builds its own `all` over the
    # examples; this one is that plus everything added here.
    all = pkgs.runCommand "easykubenix-checks" {
      checks = [
        examples.checks.all
        ekn-sandbox
        ekn-completions
        nixfmt
      ];
    } "printf '%s\\n' $checks > $out";
  };
}
