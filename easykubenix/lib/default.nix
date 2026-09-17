self: lib: rec {
  # The `mkIfExists` marker and the type that reads it. See
  # conditionalAttrsOf.nix. A module author reaches them as
  # `lib.mkIfExists`, `lib.mkIfExistsAtPath` and `lib.conditionalAttrsOf`,
  # the same way it reaches `lib.mkNamedList`.
  inherit (import ./conditionalAttrsOf.nix { inherit lib; })
    conditionalAttrsOf
    ifExistsType
    isIfExists
    mkIfExists
    mkIfExistsAtPath
    ;

  # Seeded bootstrap credentials. `envSeed` makes a reference to an
  # environment variable and `envSeeded` marks the object holding one; `ekn`
  # substitutes the value at apply time. A module author reaches both as
  # `ekn.envSeed`/`ekn.envSeeded`. The rest is for this repository's own
  # module code. See envSeed.nix.
  inherit (import ./envSeed.nix { inherit lib; })
    envSeed
    envSeedAnnotationPrefix
    envSeeded
    envSeedPrefix
    envSeedVariable
    envSeedVariables
    hasEnvSeed
    isEnvSeed
    isSeededObject
    seededVariables
    ;

  # The three markers use `_type`. This is the tag that the module system uses
  # for its own directives. `mkIf`, `mkMerge` and `mkOverride` all use it.
  # Nixpkgs ignores a `_type` value that it does not know. Such a value goes
  # through `dischargeProperties` and `pushDownProperties` without a change.
  # Thus the tag is safe. It also makes an override different in structure from
  # a Kubernetes object that is an attribute set.
  namedListType = "namedList";
  numberedListType = "numberedList";
  replaceListType = "replaceList";
  untypedType = "untyped";

  isNamedList = value: lib.isAttrs value && (value._type or null) == namedListType;
  isNumberedList = value: lib.isAttrs value && (value._type or null) == numberedListType;
  isReplaceList = value: lib.isAttrs value && (value._type or null) == replaceListType;
  isUntyped = value: lib.isAttrs value && (value._type or null) == untypedType;
  # Asks `isAttrs` once and reads `_type` once, rather than letting the two
  # predicates each do both. Measured at 238,025 duplicate `isAttrs` calls in
  # a full render -- about 2% of all evaluation calls.
  isMarkedList =
    value:
    lib.isAttrs value
    && (
      let
        marker = value._type or null;
      in
      marker == namedListType || marker == numberedListType || marker == replaceListType
    );
  # Remove the marker. This gives the bare attribute set of entries.
  stripListMarker = value: lib.removeAttrs value [ "_type" ];

  # Search a value for any of the four markers: `mkNamedList`,
  # `mkNumberedList`, `mkReplaceList` and `mkIfExists`.
  # `ekn.lib.kubeValueType` and `conditionalAttrsOf` resolve a marker when they
  # merge an option. Thus a marker can only stay in a value that goes around
  # both, such as an entry of `kubernetes.crds`. Such a marker would reach the
  # cluster as a literal `_type` field, which is not valid Kubernetes.
  # The search stops at the first marker, because `lib.any` is lazy.
  #
  # One `isAttrs` and one `_type` read per node, not three and two. Calling
  # `isMarkedList` and `isIfExists` here asked `isAttrs` twice before the
  # `else if` asked a third time, on every node of every value this walks.
  # The marker names are compared inline for that reason; the predicates stay
  # for callers with a single value to test.
  #
  # Measured on a real render (39 grafana dashboards, both arms from pinned
  # umbrellas, output byte-identical). Five frames per node became two:
  #
  #   before  hasMarker 237,837  isMarkedList 238,025  isIfExists 238,025
  #           isAttrs 238,025    isAttrs 238,025
  #   after   hasMarker 237,837  isAttrs 238,025
  #
  #   this file      1,866,790 -> 1,052,587   (-43.6%)
  #   whole render   9,540,152 -> 8,487,924   (-11.0%)
  #
  # **Those are calls, and calls are not seconds.** Measured end to end on a
  # quiet machine, ten runs each: everything that removed about 30% of this
  # render's calls bought 3.1% of the evaluation stage and 2.2% of the wall
  # clock. A module-system call costs roughly 0.1 us, and 41% of evaluator
  # time is package instantiation that no walk here touches. The exchange
  # rate is about ten to one against, so do not read a call count as a time
  # saving -- it says where the work is, not what removing it is worth.
  #
  # **Do not expect the rest of this file to shrink the same way.** What is
  # left is a floor the walk cannot avoid: `isList` at 159,294, `attrValues`
  # and `any` at 78,731 each, and `kubeAttrsToLists` at about 125,000. They
  # are one call per node, not a repeated question. Predicting two thirds
  # here was wrong for exactly that reason.
  hasMarker =
    value:
    if lib.isAttrs value then
      let
        marker = value._type or null;
      in
      marker == namedListType
      || marker == numberedListType
      || marker == replaceListType
      || marker == ifExistsType
      || lib.any hasMarker (lib.attrValues value)
    else if lib.isList value then
      lib.any hasMarker value
    else
      false;

  # Mark an attribute set as a named list.
  # A named list is a short form to override a Kubernetes list field by name.
  # Each element of such a field has a `name`.
  # The attribute name becomes the `name` of the element.
  # Each value in the input attribute set must also be an attribute set.
  mkNamedList =
    attrs:
    if !lib.isAttrs attrs then
      throw "mkNamedList error: Input must be an attribute set."
    # All children must also be attribute sets. This is a rule of the named-list
    # contract. Only objects can have a name.
    else if !(lib.all lib.isAttrs (lib.attrValues attrs)) then
      throw "mkNamedList error: All values in the attribute set must themselves be attribute sets."
    else
      attrs // { _type = namedListType; };

  # Mark a value as carried whole, never walked.
  #
  # For a value that is large, opaque and that **nobody merges field by
  # field**: a CustomResourceDefinition's OpenAPI schema, a Grafana
  # dashboard, an imported blob. `kubeValueType` walks every leaf of an
  # ordinary value through nixpkgs' merge machinery, and for these that work
  # buys nothing -- 39 dashboards cost 0.740s typed and 0.158s carried.
  #
  # **Set once.** Nothing looks inside, so nothing can merge two of them, and
  # the branch says so rather than picking one. That is the whole difference
  # from `lib.types.anything`, which measures the same here and silently
  # deep-merges a second definition -- invisible until the day someone sets
  # the value twice.
  mkUntyped = content: {
    _type = untypedType;
    inherit content;
  };

  # Mark an attribute set as a numbered list.
  # A numbered list is a short form to override a list field by index.
  # It keeps the order of the elements.
  # All keys must be integer strings, for example "0", "5" or "100".
  # Thus a sort of the keys by number gives the list again, with no data loss.
  #
  # `mkNumberedList` does not require attribute-set values, but `mkNamedList`
  # does. An index is also correct for a scalar value. This lets you override
  # `args` and `command`, for example `mkNumberedList { "1" = "--flag"; }`.
  mkNumberedList =
    attrs:
    if !lib.isAttrs attrs then
      throw "mkNumberedList error: Input must be an attribute set."
    else
      let
        keys = lib.attrNames attrs;
        # Check if every key can be successfully parsed as an integer.
        allKeysAreInts = lib.all (key: (builtins.tryEval (lib.toIntBase10 key)).success) keys;
      in
      if !allKeysAreInts then
        throw "mkNumberedList error: All keys in the attribute set must be integer strings."
      else
        attrs // { _type = numberedListType; };

  # Mark an attribute set as a list of replacements, keyed by the start of the
  # element each one replaces.
  #
  # A short form to override a list whose elements have neither a `name` nor a
  # stable index. A container's `args` is the case: the elements are bare
  # strings, and a chart bump adds a flag in front of the one you patch.
  #
  #     args = lib.mkReplaceList { "--metrics-addr=" = "--metrics-addr=:8443"; };
  #
  # The key matches an element by prefix, and the value takes the whole
  # element. An exact element is a prefix of itself, so an exact key works too.
  # Every other element and every position stay as the plain list definitions
  # wrote them.
  #
  # A key that matches no element is an error, and so is a key that matches
  # more than one. That is the property `mkNumberedList` cannot give: an index
  # is not the identity of a flag, so a list that grows an entry at the front
  # moves the override onto its neighbour and still renders.
  #
  # The input is an attribute set, and only that. `lib.mkMerge` is the list
  # form: it gives the option one definition per member, which is what the
  # merge already takes. A list argument here would merge by concatenation
  # instead of by key, and thus lose the conflict two modules get today when
  # they replace one element with two different values.
  #
  # **Sugar over `mkReplaceWhere`, and deliberately not a call to it.** The
  # two share one marker and one merge, where a key with no predicate takes
  # the prefix matcher built from itself. Writing an explicit predicate here
  # instead would give every key a `_where`, and two modules replacing one
  # flag would then collide on the predicate before the module system ever
  # compared their values -- losing the conflict message that names the key.
  mkReplaceList =
    attrs:
    if !lib.isAttrs attrs then
      throw "mkReplaceList error: Input must be an attribute set."
    # An empty key is a prefix of every element, so it can never match one.
    else if lib.elem "" (lib.attrNames attrs) then
      throw "mkReplaceList error: The empty key matches every element. Give the start of the element to replace."
    else
      attrs // { _type = replaceListType; };

  # The reserved key that carries a replacement's predicate.
  #
  # A predicate is a function, and `ekn.lib.kubeValueType` rejects a function
  # as a value. So a `where` cannot live in the entry beside its replacement:
  # the entries merge through the module system, and it would reach
  # `attrsOf`. It lives under this key instead, which the replace branch
  # removes before it merges anything. See kubeValueType.nix.
  replaceWhereKey = "_where";

  # Mark an attribute set as a list of replacements, each one addressed by a
  # predicate over the element it replaces.
  #
  # `where` runs against each element of the list the other definitions give,
  # and must pick exactly one of them:
  #
  #     tolerations = lib.mkReplaceWhere {
  #       control-plane = {
  #         where = toleration: (toleration.key or null) == "node-role.kubernetes.io/control-plane";
  #         value.effect = "NoExecute";
  #       };
  #     };
  #
  # Every other toleration the chart rendered keeps its place and its fields,
  # and so does every field of the one this picks.
  #
  # The attribute name is a label. It names the replacement in an error, and
  # it is what two definitions of the same replacement merge on. It is not
  # matched against anything, unlike `mkReplaceList`'s key.
  #
  # **`value` merges over the element it matched, and does not simply take its
  # place.** The element joins the merge as a default, leaf by leaf, so:
  #
  #   * a `value` naming three fields of a four-field object leaves the fourth
  #     alone, the way an `mkNamedList` entry does;
  #   * a field the element already sets is overridden, with no `mkForce` --
  #     the element is a default, and that is the whole point of the marker;
  #   * `value = mkForce { ... }` drops the element and takes its place whole.
  #     **That loses every field the body does not name**, which is what makes
  #     it the wholesale form and also the way to lose a field by accident;
  #   * a `value` of a string replaces the string, because a string is one
  #     leaf and the body outranks the default under it.
  #
  # `mkDefault` on the body still beats the element: 1000 is stronger than the
  # element's 1500. The rule is that the body wins unless the body is itself
  # `mkOptionDefault`.
  #
  # This is the general form. `mkReplaceList` is the short one for the common
  # case, and the two share this marker and one merge: an entry with no
  # predicate takes the prefix matcher built from its key. Both may appear on
  # one field, with distinct labels.
  mkReplaceWhere =
    attrs:
    if !lib.isAttrs attrs then
      throw "mkReplaceWhere error: Input must be an attribute set."
    else
      let
        bad = lib.filter (
          label:
          let
            entry = attrs.${label};
          in
          !(lib.isAttrs entry) || !(entry ? value) || (entry ? where && !(lib.isFunction entry.where))
        ) (lib.attrNames attrs);
      in
      if bad != [ ] then
        throw ''
          mkReplaceWhere error: each entry must be { where = <function>; value = <replacement>; },
          with `where' optional when another module already gave one for that
          label. These are not: ${lib.concatStringsSep ", " bad}.

          A property belongs on the whole marker or inside `value', never on the
          pair: `mkIf cond (mkReplaceWhere { ... })' or `value = mkForce x'. A
          property on the pair replaces it with a marker of its own, which
          carries no `value'.
        ''
      else
        (lib.mapAttrs (_: entry: entry.value) attrs)
        // {
          _type = replaceListType;
          # `null` for an entry that only overrides the value. The label still
          # has to appear here, because that is what tells the merge this label
          # is addressed by a predicate rather than by the start of its own
          # name.
          ${replaceWhereKey} = lib.mapAttrs (_: entry: entry.where or null) attrs;
        };

  # Recursively traverses a data structure, applying a transformer function to each node.
  # The traversal is pre-order (top-down), meaning a node is transformed *before* its children.
  # The transformer function receives two arguments:
  #   1. `path`: A list of strings representing the attribute path to the current node.
  #   2. `value`: The value of the current node.
  # This allows for context-aware transformations based on a node's location.
  walkWithPath =
    transformer:
    let
      go =
        path: value:
        # The transformer is applied BEFORE recursing (pre-order)
        let
          v' = transformer path value;
        in
        if lib.isAttrs v' then
          lib.mapAttrs (name: val: go (path ++ [ name ]) val) v'
        else if lib.isList v' then
          lib.imap0 (index: val: go (path ++ [ (toString index) ]) val) v'
        else
          v';
    in
    go [ ];

  # Change the entries of a named list into a standard JSON list.
  #
  # `order` gives the keys to emit, and in which order. The key is the only
  # source of the `name` attribute, so a key wins over an inner `name`.
  #
  # There are two callers, and they differ only in the order they ask for.
  # `ekn.lib.kubeValueType` takes the order from the plain list definitions it
  # merges against, so an override keeps the position of the entry it patches.
  # `kubeAttrsToLists` has no such definition to read, so it uses attribute-name
  # order. Both share this function, thus the two paths cannot drift apart.
  fromNamedAttrs =
    order: attrs:
    map (
      name:
      let
        value = attrs.${name};
      in
      if !lib.isAttrs value then
        throw "namedList error: the value for key '${name}' is not an attribute set."
      else
        value // { inherit name; }
    ) order;

  # Change the entries of a numbered list into a standard JSON list. The keys
  # are index numbers as strings, so a sort of the keys by number gives back
  # the order. Shared by `ekn.lib.kubeValueType` and `kubeAttrsToLists`.
  fromNumberedAttrs =
    attrs:
    lib.pipe attrs [
      lib.attrsToList
      (lib.sort (a: b: (lib.toInt a.name) < (lib.toInt b.name)))
      (map (x: x.value))
    ];

  # Master transformer: change marked named and numbered attribute sets back
  # into standard JSON lists.
  #
  # `ekn.lib.kubeValueType` resolves both markers when it merges an option, so
  # a value that comes out of the module system holds no marker. This function
  # is for a value that the module system never typed. A generator and a
  # transformer both run after the merge and return such a value, and
  # `kubernetes.nix` uses this function on their output for that reason.
  # One `isAttrs` and one `_type` read, for the same reason as `hasMarker`:
  # `walkWithPath` calls this on every node, and `isNamedList` followed by
  # `isNumberedList` asked both twice.
  kubeAttrsToLists =
    path: value:
    if !(lib.isAttrs value) then
      value
    else
      let
        marker = value._type or null;
      in
      if marker == namedListType then
        let
          entries = stripListMarker value;
        in
        fromNamedAttrs (lib.attrNames entries) entries
      else if marker == numberedListType then
        fromNumberedAttrs (stripListMarker value)
      else
        value;

  # md5 hash an attrset, useful to trigger rollouts by hashing ConfigMaps.
  hashAttrs = attrs: builtins.hashString "md5" (builtins.toJSON attrs);
}
