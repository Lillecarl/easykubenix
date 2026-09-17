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
    replaceWhereKey
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

  # How many characters two strings share from the start.
  #
  # **Only an error message calls these two.** They are defined here, at the
  # top of the file, so that the closure is built once at import rather than
  # per option merge. The call site keeps them inside the `throw`, where Nix
  # forces nothing until the error fires, so a render that matches every key
  # never runs them at all.
  #
  # Measured by poisoning rather than by timing: with the body of
  # `nearestElement` replaced by a `throw`, the suite gave 100 passed and 2
  # failed, and the two were the only tests that reach the no-match error. A
  # good path that forced this would have failed instead.
  commonPrefixLength =
    a: b:
    let
      limit = lib.min (lib.stringLength a) (lib.stringLength b);
      go = n: if n >= limit || lib.substring n 1 a != lib.substring n 1 b then n else go (n + 1);
    in
    go 0;

  # The element of `list` that shares the longest start with `key`, or null
  # when nothing shares more than a flag's leading dashes. Two characters is
  # the floor: every long option starts with `--`, so a shorter match names an
  # arbitrary neighbour and reads as a real suggestion.
  nearestElement =
    key: list:
    let
      scored = map (element: {
        inherit element;
        score = commonPrefixLength key element;
      }) list;
      best = lib.foldl' (a: b: if b.score > a.score then b else a) (lib.head scored) scored;
    in
    if list == [ ] || best.score <= 2 then null else best.element;

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
  # `lib.mkNamedList`, `lib.mkNumberedList`, `lib.mkReplaceList` and
  # `lib.mkReplaceWhere` markers. An attribute set with `_type = "namedList"`
  # is a short form to override by name. An attribute set with
  # `_type = "numberedList"` is a short form to override by index, and it keeps
  # the order. An attribute set with `_type = "replaceList"` replaces the
  # element a label addresses and keeps its position: by the start of the
  # label's own name, or by the predicate under `_where` that
  # `lib.mkReplaceWhere` puts there.
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
      description = "list of ${elemType.description}, or an mkNamedList/mkNumberedList/mkReplaceList/mkReplaceWhere-tagged attrset";
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

              replaceDefs = filter (def: isReplaceList def.value) defs;

              # `_where` holds functions, and this type rejects a function as a
              # value, so it must not reach the merge below. Stripped here and
              # not in `stripListMarker`, which the other two branches and
              # `kubeAttrsToLists` share.
              stripEntries =
                value:
                removeAttrs value [
                  "_type"
                  replaceWhereKey
                ];

              # Every label any definition writes. Not the merged entries: the
              # merge happens per label, below, and it needs the matched
              # element, which needs the label resolved first.
              labels = lib.unique (concatMap (def: attrNames (stripEntries def.value)) replaceDefs);

              # The definitions of one label, each keeping the file it came
              # from so a conflict names the right module.
              bodiesFor =
                label:
                concatMap (
                  def:
                  lib.optional ((stripEntries def.value) ? ${label}) (
                    def // { value = (stripEntries def.value).${label}; }
                  )
                ) replaceDefs;

              # A label may carry a predicate, from `mkReplaceWhere`. Collect
              # them per label across the definitions. Two of them cannot be
              # merged -- nothing compares two functions -- so more than one is
              # an error rather than a winner.
              # The predicates given for a label, ignoring the value-only
              # entries that record the label with a null.
              wheresFor =
                label:
                filter (where: where != null) (
                  concatMap (
                    def:
                    lib.optional (
                      (def.value.${replaceWhereKey} or { }) ? ${label}
                    ) def.value.${replaceWhereKey}.${label}
                  ) replaceDefs
                );
              # Does any definition address this label by a predicate at all,
              # including one that only overrides the value?
              whereKeyed = label: any (def: (def.value.${replaceWhereKey} or { }) ? ${label}) replaceDefs;
              # A label that one definition gives a predicate and another gives
              # as a plain key. The plain key means "match by prefix", so using
              # the predicate would silently change what that definition asked
              # for.
              plainlyKeyed =
                label:
                any (
                  def: (stripEntries def.value) ? ${label} && !((def.value.${replaceWhereKey} or { }) ? ${label})
                ) replaceDefs;

              indices = lib.genList (i: i) (lib.length base);
              # Every key matches against the plain list, never against the
              # result of the key before it. Thus the replacements happen at
              # the same time and their order cannot matter.
              #
              # `addErrorContext` because the predicate is the user's function
              # and it meets every element: `o: o.key == "x"` against a string
              # otherwise dies with "value is a string while a set was
              # expected" and names neither the label nor the element.
              matchesOf =
                label: predicate:
                filter (
                  index:
                  let
                    element = lib.elemAt base index;
                  in
                  builtins.addErrorContext "while testing the mkReplaceWhere predicate `${label}' against ${builtins.toJSON element}" (
                    predicate element
                  )
                ) indices;
              labelled = map (
                label:
                let
                  wheres = wheresFor label;
                in
                {
                  inherit label wheres;
                  explicit = whereKeyed label;
                  mixed = whereKeyed label && plainlyKeyed label;
                }
              ) labels;

              tooManyWheres = filter (entry: lib.length entry.wheres > 1) labelled;
              mixedKeying = filter (entry: entry.mixed) labelled;
              # A label addressed by a predicate that nobody ever supplied:
              # every definition of it set only a `value'.
              whereless = filter (entry: entry.explicit && entry.wheres == [ ]) labelled;
              # A label with no predicate matches by the start of its own name.
              # That is `mkReplaceList`, and it is why this branch reads a list
              # of strings unless every label carries a predicate.
              prefixKeyed = filter (entry: !entry.explicit) labelled;

              matched = map (entry: {
                key = entry.label;
                matches =
                  if entry.explicit then
                    matchesOf entry.label (lib.head entry.wheres)
                  else
                    matchesOf entry.label (element: lib.isString element && lib.hasPrefix entry.label element);
              }) labelled;

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
                and no list to replace in: ${lib.concatStringsSep ", " labels}.

                mkReplaceList patches a list that another module already defines.
                It does not define one. Two causes give this: the list is not set
                at all, or an mkForce somewhere dropped the definition that set
                it. mkReplaceList needs no mkForce, because it replaces an element
                rather than defining one.
              ''
            else if tooManyWheres != [ ] then
              throw ''
                The option `${lib.showOption loc}' has mkReplaceWhere labels defined
                with a `where' more than once: ${
                  lib.concatStringsSep ", " (map (entry: entry.label) tooManyWheres)
                }.

                Nothing compares two functions, so two predicates for one label
                cannot be merged and neither can win. Give the two replacements
                different labels, or keep one `where' and let the other
                definition write `{ value = ...; }' with no `where'.
              ''
            else if whereless != [ ] then
              throw ''
                The option `${lib.showOption loc}' has mkReplaceWhere labels that
                no definition gives a `where': ${lib.concatStringsSep ", " (map (entry: entry.label) whereless)}.

                A `{ value = ...; }' entry overrides a label that another
                definition addresses with a predicate. Here nothing does, so
                there is nothing to match. Add the `where', or use an
                mkReplaceList key.
              ''
            else if mixedKeying != [ ] then
              throw ''
                The option `${lib.showOption loc}' has labels that one definition
                gives a `where' and another gives as a plain mkReplaceList key: ${
                  lib.concatStringsSep ", " (map (entry: entry.label) mixedKeying)
                }.

                A plain key matches by the start of its own name. Taking the
                predicate instead would silently change what that definition
                asked for. Use one form for a given label.
              ''
            # Only when a label matches by its own name. A list of objects is
            # exactly what `mkReplaceWhere` is for, so a predicate must be
            # allowed to read one.
            else if prefixKeyed != [ ] && any (element: !(lib.isString element)) base then
              throw ''
                The option `${lib.showOption loc}' has an mkReplaceList definition,
                and its list holds an element that is not a string.

                mkReplaceList matches an element by the start of its own value, so
                it only reads a list of strings. Use mkReplaceWhere to match an
                object with a predicate, mkNamedList for a list of objects with a
                `name', or mkNumberedList for one without.
              ''
            else if unmatched != [ ] then
              throw ''
                The option `${lib.showOption loc}' has mkReplaceList keys that match
                no element:${
                  lib.concatMapStrings (
                    entry:
                    let
                      # A predicate has no prefix to compare, and
                      # `commonPrefixLength` reads a string, so an object list
                      # would fail inside the error. Forced only here in either
                      # case -- see `nearestElement`.
                      explicit = (wheresFor entry.key) != [ ];
                      nearest = nearestElement entry.key base;
                    in
                    "\n  ${entry.key}${
                      if explicit then
                        " -- no element satisfies its `where'"
                      else if nearest == null then
                        " -- nothing in the list resembles it"
                      else
                        " -- the closest element is ${builtins.toJSON nearest}"
                    }"
                  ) unmatched
                }

                A key is the start of the element it replaces, or a `where' is a
                predicate over it. One that matches nothing is an error and not a
                no-op, because the alternative is a component that looks patched
                and is not.

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
              let
                # **The element is a definition of the entry, not something the
                # body is pasted over.** One `mergeDefinitions` per label, with
                # the matched element first, so the module system does the
                # whole job: a body naming three fields of four leaves the
                # fourth alone, two modules writing one field conflict with a
                # message that names it, and `mkForce` on the body drops the
                # element and takes its place whole.
                #
                # The element goes in leaf by leaf at `mkOptionDefault`, so the
                # body outranks it with no `mkForce` at the call site -- which
                # is the marker's reason to exist. Wrapping the element whole
                # instead would let `filterOverrides` discard it entirely, and
                # a partial body would silently lose the other three fields.
                # A list is one leaf: a body's list replaces the element's
                # rather than appending to it, which `listOf` would do.
                asDefault =
                  v: if lib.isAttrs v && !(v ? _type) then lib.mapAttrs (_: asDefault) v else lib.mkOptionDefault v;

                entryEvaluation =
                  index: label:
                  lib.modules.mergeDefinitions (loc ++ [ label ]) elemType (
                    [
                      {
                        file = (lib.head plainDefs).file;
                        value = asDefault (lib.elemAt base index);
                      }
                    ]
                    ++ bodiesFor label
                  );
              in
              {
                headError = checkDefsForError check defs;
                value = lib.imap0 (
                  index: element:
                  let
                    label = byIndex.${toString index} or null;
                  in
                  if label == null then element else (entryEvaluation index label).mergedValue
                ) base;
                # One entry per element, the same invariant the other branches
                # keep. A replaced element takes the metadata of the merge that
                # produced it, which names every file that contributed.
                valueMeta.list = lib.imap0 (
                  index: meta:
                  let
                    label = byIndex.${toString index} or null;
                  in
                  if label == null then meta else (entryEvaluation index label).checkedAndMerged.valueMeta
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
    description = "Kubernetes-shaped JSON value (plain JSON, plus explicit mkNamedList/mkNumberedList/mkReplaceList/mkReplaceWhere override-by-name/index/prefix/predicate support)";
    emptyValue.value = null;
  };
in
valueType
