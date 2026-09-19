{
  sources ? import ./sources.nix,
  system ? builtins.currentSystem,
  pkgs ? import sources.nixpkgs {
    inherit system;
    config.allowUnfree = true;
  },
}:
let
  inherit (pkgs) lib;

  # `ekn`'s source lives in ../ekn, but its dependency closure comes from
  # nanopynix' exported development environment. See shell.nix.
  nanopynix = import sources.nanopynix { inherit pkgs sources; };

  root = import ../default.nix { inherit pkgs sources; };

  # The gates over the doc examples. Plain derivations, so they build from a
  # bare `import` -- see ../checks.nix for the entry point CI uses, and
  # ../docs/examples/default.nix for what they are.
  examples = import ../docs/examples { inherit sources system pkgs; };

  # `ekn` runs inside a Nix build sandbox from two derivations in this
  # repository -- `_jsonToYAML` and `split-manifest`, both in
  # easykubenix/internal.nix -- and a
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

  # **The two YAML readers in this repository, side by side.**
  #
  # `ekn _yamlToJson --yaml-version golike` is nanopynix's port of go-yaml's
  # own resolver into PyYAML. `ekn-yaml2json` is go-yaml itself. Nothing else
  # in either project compares them, and the scalars below are the ones that
  # can differ: each separates YAML 1.1 from YAML 1.2, and each appears in a
  # chart this repository renders.
  #
  # This gate found two that did differ, and nanopynix #306 and #307 corrected
  # the Python for both. It needs the nanopynix that carries those fixes.
  #
  # Building the package runs the Go unit tests. This gate is the
  # cross-implementation half.
  #
  # `jq -S` and not a byte diff: `ekn` writes a document's keys in the order it
  # read them and `ekn-yaml2json` writes them sorted, and `ekn` keeps the null
  # documents that Nix filters out later.
  yaml2json =
    pkgs.runCommand "easykubenix-check-yaml2json"
      {
        nativeBuildInputs = [
          root.passthru.ekn
          (pkgs.callPackage ../tools/yaml2json/package.nix { })
          pkgs.jq
        ];
      }
      ''
        cat >stream.yaml <<'YAML'
        # Source: chart/templates/nothing.yaml
        ---
        apiVersion: v1
        kind: ConfigMap
        metadata:
          name: dialect
        data:
          mode: 0644
          leadingZero: 017
          explicitOctal: 0o755
          hex: 0xff
          exponent: 1e+06
          "on": on
          "off": off
          matcher: =
          port: "8080"
          # The six classes of nanopynix #307. The deprecated reader gives
          # "n", "08", "0X1f", 90, a date object and null for these.
          bareN: n
          eight: 08
          upperHex: 0X1f
          sexagesimal: 1:30
          date: 2023-01-01
          overflow: 75.e993
        YAML

        ekn _yamlToJson --yaml-version golike <stream.yaml \
          | jq -S 'map(select(. != null))' >python.json
        ekn-yaml2json --shape list <stream.yaml | jq -S . >go.json
        diff -u python.json go.json

        # The grouped shape is what `renderChart.nix` builds in Nix. An object
        # with no `metadata.namespace` is cluster-scoped and files under
        # "none", the same key `kubernetes.resources` uses.
        ekn-yaml2json <stream.yaml | jq -S . >grouped.json
        test "$(jq -r '.resources.none.ConfigMap.dialect.data.mode' grouped.json)" = 420

        # **The two scalars this gate found, pinned by value.** The diff above
        # says the readers agree; these say what they agree on, because both
        # answers changed and a diff of two wrong answers also passes.
        #
        #   0o755   493. The Python read the string "0o755" until nanopynix
        #           #306: `_yaml11_loader` appended YAML 1.2's float resolver
        #           and not its integer one.
        #
        #   1e+06   The integer 1000000, not the float 1000000.0. go-yaml
        #           reads a float64 and Go writes an integral float64 with no
        #           decimal point, so Nix gets an integer. Helm renders a
        #           chart's `priorityClass.value: 1000000` in exactly this
        #           form, against an API field that takes int32.
        # **The write direction.** `ekn _jsonToYAML` renders what GitOps
        # commits, and go-yaml is what reads that file. A string it writes
        # plain and go-yaml resolves is a type change on an applied manifest:
        # the string "n" reached the cluster as false until nanopynix quoted
        # these. Round trip rather than an expected rendering, because the
        # question is not how it is quoted but whether it survives.
        jq -cn '[ "y", "n", "Y", "N", "08", "-0892864", "0X1f", "0O17",
                  "1:30", "0644", "1e+06", "yes", "true", "2023-01-01",
                  "yellow", "x" ] | { v: . }' >strings.json
        ekn _jsonToYAML <strings.json >strings.yaml
        ekn-yaml2json --shape list <strings.yaml | jq -S '.[0]' >back.json
        jq -S . strings.json >want-strings.json
        diff -u want-strings.json back.json

        for file in python.json go.json; do
          test "$(jq -c '.[0].data.explicitOctal' "$file")" = 493
          # `jq -c` prints the literal it read, so a float still says
          # "1000000.0" here. That is what makes this an assertion about the
          # type and not only about the value.
          test "$(jq -c '.[0].data.exponent' "$file")" = 1000000
          # Two of the six that break a chart. A `false` here and a string
          # there is the class that reaches the cluster, and the date is the
          # class that stops the document.
          test "$(jq -c '.[0].data.bareN' "$file")" = false
          test "$(jq -r '.[0].data.date' "$file")" = 2023-01-01
        done

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

  # `ekn _applyManifest` against a real single-node kubeadm cluster, under
  # User-Mode Linux. See ./kubeapply for what it asks and why the validation
  # gate cannot ask it.
  #
  # Not in `all`. A control plane is minutes of CPU even without KVM, and CI
  # builds `all` on every change; this is the gate you run when you touch
  # `ekn.apply`.
  kubeapply = import ./kubeapply { inherit pkgs lib sources; };

  # The full validation harness -- real etcd and kube-apiserver on 127.0.0.1,
  # applying the whole manifest set through `ekn _applyManifest`, then
  # kubeconform over it -- as a sandboxed derivation rather than a `nix run`.
  # Issue #16 holds the evidence that a sandbox permits everything this needs:
  # every tool is already a store-path input of the script, nothing re-enters
  # Nix, all writes land in `$TMPDIR`, and a sandboxed build binds loopback
  # TCP above port 1024 (a `runCommand` completed a full client/server round
  # trip on a fixed port there). Outbound network stays blocked, which the
  # harness never touches -- every endpoint is 127.0.0.1. What the sandbox
  # did break was kubeadm's and kube-apiserver's default route lookups, fixed
  # in validation.nix; see there.
  #
  # `HOME` points inside the build tree so fish has a writable state
  # directory -- the builder's default (`/homeless-shelter`) is read-only,
  # and fish prints three alarming-but-harmless `error:` blocks without it.
  # Measured cost: ~10s per build, so `all` carries it.
  validation-e2e =
    pkgs.runCommand "easykubenix-check-validation-e2e"
      {
        HOME = "/build/home";
      }
      ''
        ${examples.packages.validationScript}/bin/kubeval
        touch "$out"
      '';

  # The bootstrap example's own instance over the same harness -- ArgoCD's
  # CRDs and the Application that needs them. `kubernetes.generated` excludes
  # a deployment unit's objects by design, so `validation-e2e` can never
  # cover them; this is their gate.
  bootstrap-validation-e2e =
    pkgs.runCommand "easykubenix-check-bootstrap-validation-e2e"
      {
        HOME = "/build/home";
      }
      ''
        ${examples.packages.bootstrapValidationScript}/bin/kubeval
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
  # A `class = "tf"` deployment unit, rendered and run through `tofu validate`
  # in the sandbox. In `all`: it builds one small configuration and one
  # provider, both of which the nixpkgs pin already carries. See ./tofu.
  tofu-render = pkgs.callPackage ./tofu { inherit sources; };

  # Providers from the OpenTofu registry rather than nixpkgs. Outside `all`:
  # it fetches a ~424M index plus a provider zip each. See ./tofu/registry.nix.
  tofu-registry = pkgs.callPackage ./tofu/registry.nix { inherit sources; };

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
    kubeapply
    nixfmt
    tofu-render
    tofu-registry
    validation-e2e
    bootstrap-validation-e2e
    ;
  inherit (examples) packages;

  shell = pkgs.python3Packages.callPackage ./shell.nix {
    inherit (root.passthru) eknDevEnv;
  };

  checks = examples.checks // {
    inherit
      ekn-sandbox
      ekn-completions
      yaml2json
      kubeapply
      nixfmt
      tofu-render
      tofu-registry
      validation-e2e
      bootstrap-validation-e2e
      ;
    # `all` is what CI builds, so a gate that is not in it is a gate that does
    # not run. ../docs/examples/default.nix builds its own `all` over the
    # examples; this one is that plus everything added here.
    all = pkgs.runCommand "easykubenix-checks" {
      checks = [
        examples.checks.all
        ekn-sandbox
        ekn-completions
        yaml2json
        nixfmt
        tofu-render
        validation-e2e
        bootstrap-validation-e2e
      ];
    } "printf '%s\\n' $checks > $out";
  };
}
