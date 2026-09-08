{ lib }:
let
  # The whole Nix-side contribution to seeded credentials: a helper that
  # makes a reference, and the predicates that find one again.
  #
  # A secret must never enter Nix. Nix can write an evaluated string to
  # `/nix/store`, which is world-readable and persists, and `ekn.cacheTo`
  # pushes that closure to a binary cache. So nothing here reads an
  # environment variable, and nothing here resolves one. Evaluation only ever
  # handles the *name* of a variable. The `ekn` CLI substitutes the value at
  # apply time, against a manifest that has already been rendered.
  #
  # The reference is a plain string, not a marked attribute set. That is not
  # a style choice. A Kubernetes `Secret`'s `stringData` is
  # `map[string]string`, so an attribute set there makes the rendered
  # manifest fail schema validation -- and `ekn validate` pipes
  # `internal.manifestJSONFile` straight into `kubeconform` in shell (see
  # validation.nix), outside the CLI, where nothing can substitute first.
  # A string sentinel keeps every manifest valid, so validation needs no stub
  # and no skip list.
  prefix = "$ekn:env:";

  # A conservative POSIX-ish environment variable name. Rejecting a bad name
  # here turns a reference nothing can ever resolve into an evaluation error,
  # rather than a literal sentinel reaching a cluster.
  isVariableName = name: name != "" && builtins.match "[A-Za-z_][A-Za-z0-9_]*" name != null;

  envSeed =
    variable:
    if !lib.isString variable then
      throw "ekn.envSeed error: the variable name must be a string."
    else if !isVariableName variable then
      throw ''
        ekn.envSeed error: "${variable}" is not a usable environment variable
        name. Use letters, digits and underscore, and do not start with a
        digit.
      ''
    else
      "${prefix}${variable}";

  isEnvSeed = value: lib.isString value && lib.hasPrefix prefix value;

  envSeedVariable =
    value:
    if !isEnvSeed value then
      throw "envSeedVariable error: the value is not an ekn.envSeed reference."
    else
      lib.removePrefix prefix value;

  # Search a value for a reference. The search stops at the first one,
  # because `lib.any` is lazy.
  #
  # Every caller gates this on the object's `kind` first. The scope is
  # Secrets, so a string compare rejects almost every object before this walk
  # starts. This runs once per object the author marks with `envSeeded`, and
  # never over a whole resource set: see `isSeededObject` below for why.
  hasEnvSeed =
    value:
    if isEnvSeed value then
      true
    else if lib.isAttrs value then
      lib.any hasEnvSeed (lib.attrValues value)
    else if lib.isList value then
      lib.any hasEnvSeed value
    else
      false;

  # Collect every variable a value refers to, in a stable order.
  envSeedVariables =
    value:
    lib.unique (
      if isEnvSeed value then
        [ (envSeedVariable value) ]
      else if lib.isAttrs value then
        lib.concatMap envSeedVariables (lib.attrValues value)
      else if lib.isList value then
        lib.concatMap envSeedVariables value
      else
        [ ]
    );

  annotationPrefix = "ekn.dev/env-";

  # Declare an object seeded, and record which variables it needs.
  #
  #   kubernetes.objects.argocd.Secret.repo-creds = ekn.envSeeded {
  #     stringData.password = ekn.envSeed "ARGOCD_REPO_PASSWORD";
  #     ...
  #   };
  #
  # This is the one deep walk in the design, and it runs only over the
  # objects an author marks -- two, in the case this exists for. Everything
  # downstream reads the annotations instead: `ekn.dev/env-0`, `ekn.dev/env-1`
  # and so on, one per variable.
  #
  # Numbered keys rather than one packed value, so `kubectl get secret -o json
  # | grep ekn.dev/env-` answers "which live objects are seeded, and from
  # what" without parsing anything.
  envSeeded =
    object:
    let
      variables = envSeedVariables object;
      annotations = lib.listToAttrs (
        lib.imap0 (index: variable: {
          name = "${annotationPrefix}${toString index}";
          value = variable;
        }) variables
      );
    in
    if variables == [ ] then
      throw ''
        ekn.envSeeded error: this object holds no ekn.envSeed reference, so
        there is nothing to seed. Drop the `envSeeded' wrapper, or add a
        reference with `ekn.envSeed "VARNAME"'.
      ''
    else
      lib.recursiveUpdate object { metadata.annotations = annotations; };

  # Whether a rendered object is seeded. Shallow and bounded: it reads the
  # annotations `envSeeded` wrote and never descends into the object.
  #
  # The exportable filter, the kluctl exclusion and the GitOps assertion all
  # go through here, so they cannot drift apart -- and none of them pays a
  # recursive walk over a whole resource set to answer the question.
  isSeededObject =
    object:
    lib.any (name: lib.hasPrefix annotationPrefix name) (
      lib.attrNames (object.metadata.annotations or { })
    );

  # The variables a rendered object declares, read back from its annotations.
  seededVariables =
    object:
    let
      annotations = object.metadata.annotations or { };
    in
    map (name: annotations.${name}) (
      lib.naturalSort (lib.filter (lib.hasPrefix annotationPrefix) (lib.attrNames annotations))
    );
in
{
  inherit
    envSeed
    envSeeded
    envSeedVariable
    envSeedVariables
    hasEnvSeed
    isEnvSeed
    isSeededObject
    seededVariables
    ;
  envSeedPrefix = prefix;
  envSeedAnnotationPrefix = annotationPrefix;
}
