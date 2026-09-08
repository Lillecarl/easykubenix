let
  sources = import ../nix/sources.nix;
  pkgs = import sources.nixpkgs { };
  # `envSeed` and its predicates live in easykubenix's own `lib.extend`
  # overlay, not plain nixpkgs `lib` -- extend the same way
  # `easykubenix/pkgs/default.nix` does for the real module evaluation.
  lib = pkgs.lib.extend (import ../easykubenix/lib);

  secret = {
    apiVersion = "v1";
    kind = "Secret";
    metadata.name = "repo-creds";
    stringData = {
      type = "git";
      url = "https://example.com/group/repo.git";
      username = "ci-token";
      password = lib.envSeed "ARGOCD_REPO_PASSWORD";
    };
  };

  # Two references on one object, to exercise the numbering.
  twoVariables = {
    apiVersion = "v1";
    kind = "Secret";
    metadata.name = "repo-creds";
    stringData = {
      password = lib.envSeed "ARGOCD_REPO_PASSWORD";
      sshPrivateKey = lib.envSeed "ARGOCD_REPO_SSHKEY";
    };
  };

  plainSecret = {
    apiVersion = "v1";
    kind = "Secret";
    metadata.name = "ordinary";
    stringData.password = "hunter2";
  };

  # A reference nested under a list, to prove the walk reaches one.
  nestedInList = {
    apiVersion = "v1";
    kind = "Secret";
    metadata.name = "nested";
    spec.items = [
      { a = "plain"; }
      { b = lib.envSeed "NESTED_VARIABLE"; }
    ];
  };

  # These must throw. Each is a thunk, so the test can assert on the error.
  emptyNameThrows = lib.envSeed "";
  leadingDigitThrows = lib.envSeed "1BAD";
  punctuationThrows = lib.envSeed "BAD-NAME";
  nonStringThrows = lib.envSeed [ "NOT_A_STRING" ];
  variableOfNonSeedThrows = lib.envSeedVariable "just a string";
  markingWithoutAReferenceThrows = lib.envSeeded plainSecret;
in
{
  # The reference is a plain string. This is what keeps a rendered manifest
  # schema-valid, so `kubeconform` and `ekn validate` need no special case.
  referenceIsAString = lib.isString (lib.envSeed "ARGOCD_REPO_PASSWORD");
  reference = lib.envSeed "ARGOCD_REPO_PASSWORD";
  prefix = lib.envSeedPrefix;

  recognisesItsOwnReference = lib.isEnvSeed (lib.envSeed "ARGOCD_REPO_PASSWORD");
  rejectsAnOrdinaryString = !(lib.isEnvSeed "hunter2");
  rejectsANonString = !(lib.isEnvSeed { password = "x"; });
  readsTheVariableBack = lib.envSeedVariable (lib.envSeed "ARGOCD_REPO_PASSWORD");

  findsAReferenceInAnObject = lib.hasEnvSeed secret;
  findsAReferenceUnderAList = lib.hasEnvSeed nestedInList;
  findsNothingInAPlainObject = !(lib.hasEnvSeed plainSecret);

  # `envSeeded` does the one deep walk, over an object its author marked.
  # Everything downstream reads the annotations it writes.
  annotationsFromOneVariable = (lib.envSeeded secret).metadata.annotations;
  annotationsAreNumbered = (lib.envSeeded twoVariables).metadata.annotations;
  markingKeepsTheObject = (lib.envSeeded secret).stringData;

  # The shallow, bounded test every consumer uses. An unmarked object is not
  # seeded, however many references it holds -- the annotation is the
  # contract, precisely so that nothing has to walk a resource set.
  seededObjectReadsTheAnnotation = lib.isSeededObject (lib.envSeeded secret);
  seededObjectIgnoresAnUnmarkedObject = !(lib.isSeededObject secret);
  seededObjectIgnoresAPlainSecret = !(lib.isSeededObject plainSecret);
  variablesReadBackFromAnnotations = lib.seededVariables (lib.envSeeded twoVariables);
  collectsVariablesInOrder = lib.envSeedVariables twoVariables;

  inherit
    emptyNameThrows
    leadingDigitThrows
    punctuationThrows
    nonStringThrows
    variableOfNonSeedThrows
    markingWithoutAReferenceThrows
    ;
}
