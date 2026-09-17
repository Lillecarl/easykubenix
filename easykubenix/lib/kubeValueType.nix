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
    isReplaceList
    isUntyped
    stripListMarker
    # Both directions back to a list are shared with `kubeAttrsToLists`, the
    # pass kubernetes.nix runs over generator and transformer output. See
    # lib/default.nix.
    fromNamedAttrs
    fromNumberedAttrs
    ;

  # The marker names, for the checks that read `_type` once and compare it
  # against all of them. `untypedMarker` is renamed, because `untypedType`
  # below is this file's option type for such a value.
  inherit (lib) namedListType numberedListType replaceListType;
  untypedMarker = lib.untypedType;

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
  # `lib.mkNamedList`, `lib.mkNumberedList` and `lib.mkReplaceList` markers. An
  # attribute set with `_type = "namedList"` is a short form to override by
  # name. An attribute set with `_type = "numberedList"` is a short form to
  # override by index, and it keeps the order. An attribute set with
  # `_type = "replaceList"` replaces an element that starts with the key, and
  # keeps its position.
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
      description = "list of ${elemType.description}, or an mkNamedList/mkNumberedList/mkReplaceList-tagged attrset";
      # A callable check with `isV2MergeCoherent`. The flag tells the module
      # system that this check agrees with this merge, so `either`/`oneOf` can
      # pick a branch without running `checkV2MergeCoherence` over every
      # definition. That check is the dominant cost in a Kubernetes-shaped
      # value tree; see the note on `kubernetes.crds` in kubernetes.nix.
      #
      # One `isAttrs` and one `_type` read for an attribute set, not one of
      # each per marker. `hasMarker` and `kubeAttrsToLists` in lib/default.nix
      # are written this way for the same reason: this runs on every node of
      # the tree, and an attribute set is a third of them.
      check = {
        __functor =
          _: value:
          if isList value then
            true
          else if lib.isAttrs value then
            let
              marker = value._type or null;
            in
            marker == namedListType || marker == numberedListType || marker == replaceListType
          else
            false;
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
            anyReplace = any (def: isReplaceList def.value) defs;

            # Reject `mkBefore`, `mkAfter` and `mkOrder` on an entry of a marked
            # list. The module system sorts the definitions before it calls this
            # merge. A marked list then takes its order from the keys, so the sort
            # has no effect. An error is better than a silent loss of the order.
            #
            # Every branch, because none of them takes its order from the sort.
            # This used to read `isNamedList` alone, so a numbered list
            # discharged the property and merged the entry as if nobody had
            # written it. A replace entry has no order of its own at all: it
            # takes the position of the element it replaces.
            orderedEntryKeys = concatMap (
              def:
              if isNamedList def.value || isNumberedList def.value || isReplaceList def.value then
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

            markersPresent =
              lib.optional anyNamed "mkNamedList"
              ++ lib.optional anyNumbered "mkNumberedList"
              ++ lib.optional anyReplace "mkReplaceList";
          in
          if lib.length markersPresent > 1 then
            throw ''
              The option `${lib.showOption loc}' has more than one list marker:
              ${lib.concatStringsSep ", " markersPresent}. Use one marker for a
              given field. mkNamedList addresses an entry by its `name'.
              mkNumberedList addresses an entry by its index. mkReplaceList
              addresses an entry by the start of its own value.
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
          else if anyReplace && orderedEntryKeys != [ ] then
            throw ''
              The option `${lib.showOption loc}' uses mkBefore/mkAfter/mkOrder on
              the mkReplaceList entries: ${lib.concatStringsSep ", " orderedEntryKeys}.
              A replacement takes the position of the element it replaces, so an
              order property on an entry does nothing. Use mkNumberedList to set
              an order.
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
          else if anyReplace then
            let
              # The plain definitions merge as an ordinary list, so they still
              # concatenate the way `listOf` concatenates them. The other two
              # branches cannot do this: they read every definition as entries
              # of one attribute set, which makes two plain definitions collide
              # on a key instead of following each other.
              plainDefs = filter (def: !(isReplaceList def.value)) defs;
              baseEvaluation = lib.modules.mergeDefinitions loc listType plainDefs;
              base = baseEvaluation.mergedValue;

              # The replacements merge as an attribute set, so two modules that
              # patch the same element conflict with the module system's own
              # message, and `mkForce` on one entry works.
              replaceEvaluation = lib.modules.mergeDefinitions loc attrsType (
                map (def: def // { value = stripListMarker def.value; }) (
                  filter (def: isReplaceList def.value) defs
                )
              );
              replacements = replaceEvaluation.mergedValue;

              indices = lib.genList (i: i) (lib.length base);
              # Every key matches against the plain list, never against the
              # result of the key before it. Thus the replacements happen at
              # the same time and their order cannot matter.
              matchesOf = key: filter (index: lib.hasPrefix key (lib.elemAt base index)) indices;
              matched = map (key: {
                inherit key;
                matches = matchesOf key;
              }) (attrNames replacements);

              unmatched = filter (entry: entry.matches == [ ]) matched;
              ambiguous = filter (entry: lib.length entry.matches > 1) matched;
              byIndex = listToAttrs (
                map (entry: {
                  name = toString (lib.head entry.matches);
                  value = entry.key;
                }) matched
              );
              # Checked after `ambiguous`, so every entry has exactly one match.
              # `byIndex` keeps the first key of a collision, so it cannot find
              # both sides on its own.
              firstMatches = map (entry: lib.head entry.matches) matched;
              collided = filter (
                entry: lib.count (index: index == lib.head entry.matches) firstMatches > 1
              ) matched;

              showList = lib.concatMapStrings (element: "\n  ${builtins.toJSON element}") base;
            in
            if plainDefs == [ ] then
              throw ''
                The option `${lib.showOption loc}' has an mkReplaceList definition
                and no list to replace in: ${lib.concatStringsSep ", " (attrNames replacements)}.

                mkReplaceList patches a list that another module already defines.
                It does not define one. Two causes give this: the list is not set
                at all, or an mkForce somewhere dropped the definition that set
                it. mkReplaceList needs no mkForce, because it replaces an element
                rather than defining one.
              ''
            else if any (element: !(lib.isString element)) base then
              throw ''
                The option `${lib.showOption loc}' has an mkReplaceList definition,
                and its list holds an element that is not a string.

                mkReplaceList matches an element by the start of its own value, so
                it only reads a list of strings. Use mkNamedList for a list of
                objects with a `name', or mkNumberedList for one without.
              ''
            else if unmatched != [ ] then
              throw ''
                The option `${lib.showOption loc}' has mkReplaceList keys that match
                no element: ${lib.concatStringsSep ", " (map (entry: entry.key) unmatched)}.

                A key is the start of the element it replaces. A key that matches
                nothing is an error and not a no-op, because the alternative is a
                component that looks patched and is not.

                The list holds:${showList}
              ''
            else if ambiguous != [ ] then
              throw ''
                The option `${lib.showOption loc}' has mkReplaceList keys that match
                more than one element: ${lib.concatStringsSep ", " (map (entry: entry.key) ambiguous)}.

                Give a longer key, or use mkNumberedList to address one element by
                its index.

                The list holds:${showList}
              ''
            else if collided != [ ] then
              throw ''
                The option `${lib.showOption loc}' has two mkReplaceList keys that
                match the same element: ${lib.concatStringsSep ", " (map (entry: entry.key) collided)}.

                One element takes one replacement. Keep the key you mean.

                The list holds:${showList}
              ''
            else
              {
                headError = checkDefsForError check defs;
                value = lib.imap0 (
                  index: element:
                  let
                    key = byIndex.${toString index} or null;
                  in
                  if key == null then element else replacements.${key}
                ) base;
                # One entry per element, the same invariant the other branches
                # keep. A replaced element takes the metadata of the definition
                # that replaced it, because that is the file a reader has to
                # open to change it.
                valueMeta.list = lib.imap0 (
                  index: meta:
                  let
                    key = byIndex.${toString index} or null;
                  in
                  if key == null then meta else replaceEvaluation.checkedAndMerged.valueMeta.attrs.${key}
                ) baseEvaluation.checkedAndMerged.valueMeta.list;
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
  # the same reason this guard rejects the list markers.
  # The untyped marker is in this guard for the same reason as the list markers,
  # and it was found the same way the comment above predicts -- by a differing
  # leaf count (37,455 against 37,416), not by reading. Without it the
  # attribute map accepts an untyped marker, merges it as a plain object, and
  # `_type` and `content` reach the rendered manifest.
  # One `_type` read for every marker, for the reason `namedListOf.check`
  # gives. `addCheck` runs `conditionalAttrsOf`'s own check first, so the
  # value is an attribute set here.
  objectType = types.addCheck (conditionalAttrsOf valueType) (
    x:
    let
      marker = x._type or null;
    in
    marker != namedListType
    && marker != numberedListType
    && marker != replaceListType
    && marker != untypedMarker
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
    description = "Kubernetes-shaped JSON value (plain JSON, plus explicit mkNamedList/mkNumberedList/mkReplaceList override-by-name/index/content support)";
    emptyValue.value = null;
  };
in
valueType
