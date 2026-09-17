{ lib }:
let
  inherit (lib)
    mkOptionType
    types
    isList
    any
    listToAttrs
    removeAttrs
    attrNames
    concatMap
    filter
    elem
    ;

  inherit (lib)
    conditionalAttrsOf
    isNamedList
    isNumberedList
    isUntyped
    stripListMarker
    # Both directions back to a list are shared with `kubeAttrsToLists`, the
    # pass kubernetes.nix runs over generator and transformer output. See
    # lib/default.nix.
    fromNamedAttrs
    fromNumberedAttrs
    ;

  # The v2 merge protocol asks a type for its own head error instead of
  # throwing from inside `merge`. This is nixpkgs' own shape, copied because
  # `lib.types` does not export it.
  checkDefsForError =
    check: definitions:
    if lib.all (definition: check definition.value) definitions then
      null
    else
      {
        message = "Definition values: ${
          lib.options.showDefs (lib.filter (definition: !check definition.value) definitions)
        }";
      };

  # A plain Kubernetes list becomes an attribute set with the `name` of each
  # element as the key. The `name` itself does not stay in the value. The key
  # is the only source of the name. `kubeAttrsToLists` uses the same rule.
  toNamedAttrs =
    list:
    listToAttrs (
      map (e: {
        inherit (e) name;
        value = removeAttrs e [ "name" ];
      }) list
    );
  toNumberedAttrs =
    list:
    listToAttrs (
      lib.imap0 (i: v: {
        name = toString i;
        value = v;
      }) list
    );
  # A `listOf elemType` alternative. It also accepts the explicit
  # `lib.mkNamedList` and `lib.mkNumberedList` markers. An attribute set with
  # `_type = "namedList"` is a short form to override by name. An attribute set
  # with `_type = "numberedList"` is a short form to override by index, and it
  # keeps the order.
  #
  # The behavior is opt-in. It is not a guess from the shape of the data.
  # A Kubernetes field has one fixed shape. It is always a list. It is never a
  # map. Thus this type must never read a plain attribute set as "the map form
  # of a list", even if the values look like list elements.
  #
  # An earlier version did guess from the shape. It read any list of attribute
  # sets with a `name` field as a candidate for an override by name. This was
  # wrong. It broke `metadata.ownerReferences`, where two entries can share a
  # `name` but have a different owner `kind`. One of the two entries was lost.
  # The same guess also read a typo (one object in place of a list) as a valid
  # named list. Only a module that calls `lib.mkNamedList` or
  # `lib.mkNumberedList` gets this merge behavior. Every other list behaves
  # exactly like a plain `listOf`.
  #
  # The merge reconciles a plain list definition against a later marked
  # override. A producer such as helm.nix or importyaml.nix can emit a plain
  # rendered list. It does not have to convert its output first.
  namedListOf =
    elemType:
    let
      attrsType = types.attrsOf elemType;
      listType = types.listOf elemType;
    in
    mkOptionType rec {
      name = "namedListOf";
      description = "list of ${elemType.description}, or an mkNamedList/mkNumberedList-tagged attrset";
      # A callable check with `isV2MergeCoherent`. The flag tells the module
      # system that this check agrees with this merge, so `either`/`oneOf` can
      # pick a branch without running `checkV2MergeCoherence` over every
      # definition. That check is the dominant cost in a Kubernetes-shaped
      # value tree; see the note on `kubernetes.crds` in kubernetes.nix.
      check = {
        __functor = _: value: listType.check value || isNamedList value || isNumberedList value;
        isV2MergeCoherent = true;
      };
      merge = rec {
        # The legacy entrypoint stays callable, and delegates. A caller that
        # still does `type.merge loc defs` gets the same value.
        __functor =
          self: loc: defs:
          (self.v2 { inherit loc defs; }).value;
        v2 =
          { loc, defs }:
          let
            anyNamed = any (def: isNamedList def.value) defs;
            anyNumbered = any (def: isNumberedList def.value) defs;

            # Reject `mkBefore`, `mkAfter` and `mkOrder` on an entry of a marked
            # list. The module system sorts the definitions before it calls this
            # merge. A marked list then takes its order from the keys, so the sort
            # has no effect. An error is better than a silent loss of the order.
            #
            # Both branches, because both take their order from their keys. This
            # used to read `isNamedList` alone, so a numbered list discharged the
            # property and merged the entry as if nobody had written it.
            orderedEntryKeys = concatMap (
              def:
              if isNamedList def.value || isNumberedList def.value then
                filter (key: ((def.value.${key} or null)._type or null) == "order") (
                  attrNames (stripListMarker def.value)
                )
              else
                [ ]
            ) defs;

            # Merge the entries as an attribute set, whatever form each
            # definition arrived in. `mergeDefinitions` rather than
            # `attrsType.merge`, because only it also returns the per-entry
            # metadata this type has to pass upwards.
            attrsEvaluation =
              transform:
              lib.modules.mergeDefinitions loc attrsType (
                map (def: def // { value = transform def.value; }) defs
              );
          in
          if anyNamed && anyNumbered then
            throw ''
              The option `${lib.showOption loc}' has both an mkNamedList and an
              mkNumberedList definition. Use only one of the two for a given
              field. mkNamedList addresses an entry by its `name'. mkNumberedList
              addresses an entry by its index.
            ''
          else if anyNamed && orderedEntryKeys != [ ] then
            throw ''
              The option `${lib.showOption loc}' uses mkBefore/mkAfter/mkOrder on
              the mkNamedList entries: ${lib.concatStringsSep ", " orderedEntryKeys}.
              A named list takes its order from the plain list definitions. It
              appends a new name at the end. Use mkNumberedList to set an order.
            ''
          else if anyNumbered && orderedEntryKeys != [ ] then
            throw ''
              The option `${lib.showOption loc}' uses mkBefore/mkAfter/mkOrder on
              the mkNumberedList entries: ${lib.concatStringsSep ", " orderedEntryKeys}.
              A numbered list takes its order from its index keys, so an order
              property on an entry does nothing. Give the entry the index you
              want it at.
            ''
          else if anyNamed then
            let
              evaluation = attrsEvaluation (
                value: if isList value then toNamedAttrs value else stripListMarker value
              );
              merged = evaluation.mergedValue;
              # Keep the order of the plain list definitions. A name that only a
              # marker introduces goes after them, in attribute-name order.
              # `attrValues` alone would sort every name and thus silently
              # reorder a list that a module only wanted to patch.
              listKeys = concatMap (def: if isList def.value then map (e: e.name) def.value else [ ]) defs;
              newKeys = filter (key: !(elem key listKeys)) (attrNames merged);
              order = filter (key: merged ? ${key}) (lib.unique (listKeys ++ newKeys));
            in
            {
              headError = checkDefsForError check defs;
              # Put the key back as the `name` of the element. The key wins over
              # an inner `name`. `kubeAttrsToLists` shares this function, so it
              # applies the same rule.
              value = fromNamedAttrs order merged;
              # The metadata is a list, in the order the value has, because that
              # is the shape a reader of this option sees.
              valueMeta.list = map (key: evaluation.checkedAndMerged.valueMeta.attrs.${key}) order;
            }
          else if anyNumbered then
            let
              evaluation = attrsEvaluation (
                value: if isList value then toNumberedAttrs value else stripListMarker value
              );
              merged = evaluation.mergedValue;
              order = map (entry: entry.name) (
                lib.sort (a: b: (lib.toInt a.name) < (lib.toInt b.name)) (lib.attrsToList merged)
              );
            in
            {
              headError = checkDefsForError check defs;
              value = fromNumberedAttrs merged;
              valueMeta.list = map (key: evaluation.checkedAndMerged.valueMeta.attrs.${key}) order;
            }
          else
            (lib.modules.mergeDefinitions loc listType defs).checkedAndMerged;
      };
      nestedTypes.elemType = elemType;
    };

  # A marked attribute set must never type-check as a plain JSON object.
  # `types.oneOf` is a left fold of `either`, and `either` uses the first branch
  # that all definitions pass. Without this guard, the attribute map would
  # accept a marked attribute set, because its own check is only `isAttrs`. A
  # lone marked definition would then merge as an object and keep its `_type`
  # marker in the output.
  #
  # `conditionalAttrsOf` and not `attrsOf`, so `lib.mkIfExists` works at every
  # field of an object, not only at the namespace/Kind/name levels above it.
  # A module can then patch `spec.replicas` only when that field is already
  # there. `conditionalAttrsOf` rejects a bare `mkIfExists` marker itself, for
  # the same reason this guard rejects the two list markers.
  # `isUntyped` is in this guard for the same reason as the two list markers,
  # and it was found the same way the comment above predicts -- by a differing
  # leaf count (37,455 against 37,416), not by reading. Without it the
  # attribute map accepts an untyped marker, merges it as a plain object, and
  # `_type` and `content` reach the rendered manifest.
  objectType = types.addCheck (conditionalAttrsOf valueType) (
    x: !(isNamedList x) && !(isNumberedList x) && !(isUntyped x)
  );

  # A value carried whole. See `lib.mkUntyped`.
  #
  # **Two definitions are an error, not a merge.** Nothing looks inside, so
  # nothing can merge them, and saying so is the whole difference from
  # `types.anything` -- which measures identically here and silently
  # deep-merges the second definition.
  #
  # A legacy `merge` is correct here rather than an oversight: `either` handles
  # a branch without `merge.v2` by calling `merge loc defs` and computing
  # `headError` from `check`. There is no metadata to preserve, because the
  # value is never walked.
  untypedType = mkOptionType {
    name = "untypedValue";
    description = "a value carried whole, never walked";
    check = isUntyped;
    merge =
      loc: defs:
      if builtins.length defs == 1 then
        (builtins.head defs).value.content
      else
        throw (
          "The option `${lib.showOption loc}` is untyped and has ${toString (builtins.length defs)} "
          + "definitions. An untyped value is taken whole, so it can be set once."
        );
  };

  # `types.nullOr` still uses the legacy merge protocol, so it returns no
  # metadata. It sits at the outermost boundary of this recursive type, so
  # using it would discard the metadata of the whole tree below. This branch
  # is `nullOr`'s null half, written to the v2 protocol.
  #
  # The behaviour is `nullOr`'s: all definitions null gives null, and a mix of
  # null and non-null is an error. `oneOf` produces the second half, because no
  # single branch accepts both a null and an object.
  nullType = mkOptionType rec {
    name = "null";
    description = "null";
    check = {
      __functor = _: value: value == null;
      isV2MergeCoherent = true;
    };
    merge = rec {
      __functor =
        self: loc: defs:
        (self.v2 { inherit loc defs; }).value;
      v2 =
        { defs, ... }:
        {
          headError = checkDefsForError check defs;
          value = null;
          valueMeta = { };
        };
    };
    emptyValue.value = null;
  };

  # **This list is ordered by measured frequency, and the order is a
  # performance decision rather than a taste one.** `types.oneOf` is a left
  # fold of `either`, and `either`'s v2 merge forces its first branch's
  # `headError` before it looks at the second, so a value matching at
  # position k pays k branch checks.
  #
  # Counted over every node of a real 919-object render (269,929 nodes):
  # strings 58.8%, attribute sets 32.1%, lists 5.1%, ints 2.8%, bools 1.1%,
  # null 0.02% (44 nodes in the whole render), floats none at all. The old
  # order checked the two commonest last and `null` first for every value:
  # 5.97 expected branch checks per node against 1.55 for this one. Worth
  # 16% on the set the type walks today, and 23% on CRD-heavy data.
  #
  # **Two of these orderings are load-bearing and must not be sorted away by
  # a later frequency count.** Both have tests, because a comment does not
  # survive somebody re-sorting the list:
  #
  # - `types.str` before `types.path`: `path.check` accepts a string
  #   beginning with "/", so every absolute-path-looking string in a
  #   manifest would otherwise merge as a path.
  # - `namedListOf` before `objectType`: a plain JSON object fails the
  #   former's check, so an object still falls through to the latter.
  baseType = types.oneOf [
    types.str
    (namedListOf valueType)
    objectType
    types.int
    types.bool
    nullType
    types.float
    types.path
    # **Last, and measured rather than chosen.** On a 269,929-node tree with
    # no untyped values in it at all -- pure tax -- the branch costs +8%
    # first and +2% last. An untyped value is found a few hundred times in a
    # whole render, so the checks it then pays do not signify, while every
    # ordinary value is untouched by a branch it never reaches.
    untypedType
  ];
  valueType = baseType // {
    description = "Kubernetes-shaped JSON value (plain JSON, plus explicit mkNamedList/mkNumberedList override-by-name/index support)";
    emptyValue.value = null;
  };
in
valueType
