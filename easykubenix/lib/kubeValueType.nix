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
    isNamedList
    isNumberedList
    stripListMarker
    # Both directions back to a list are shared with `kubeAttrsToLists`, the
    # pass kubernetes.nix runs over generator and transformer output. See
    # lib/default.nix.
    fromNamedAttrs
    fromNumberedAttrs
    ;

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
    mkOptionType {
      name = "namedListOf";
      description = "list of ${elemType.description}, or an mkNamedList/mkNumberedList-tagged attrset";
      check = x: listType.check x || isNamedList x || isNumberedList x;
      merge =
        loc: defs:
        let
          anyNamed = any (def: isNamedList def.value) defs;
          anyNumbered = any (def: isNumberedList def.value) defs;

          # Reject `mkBefore`, `mkAfter` and `mkOrder` on an entry of a named
          # list. The module system sorts the definitions before it calls this
          # merge. A named list then takes its order from the keys, so the sort
          # has no effect. An error is better than a silent loss of the order.
          orderedNamedKeys = concatMap (
            def:
            if isNamedList def.value then
              filter (key: ((def.value.${key} or null)._type or null) == "order") (
                attrNames (stripListMarker def.value)
              )
            else
              [ ]
          ) defs;
        in
        if anyNamed && anyNumbered then
          throw ''
            The option `${lib.showOption loc}' has both an mkNamedList and an
            mkNumberedList definition. Use only one of the two for a given
            field. mkNamedList addresses an entry by its `name'. mkNumberedList
            addresses an entry by its index.
          ''
        else if anyNamed && orderedNamedKeys != [ ] then
          throw ''
            The option `${lib.showOption loc}' uses mkBefore/mkAfter/mkOrder on
            the mkNamedList entries: ${lib.concatStringsSep ", " orderedNamedKeys}.
            A named list takes its order from the plain list definitions. It
            appends a new name at the end. Use mkNumberedList to set an order.
          ''
        else if anyNamed then
          let
            merged = attrsType.merge loc (
              map (
                def:
                def
                // {
                  value = if isList def.value then toNamedAttrs def.value else stripListMarker def.value;
                }
              ) defs
            );
            # Keep the order of the plain list definitions. A name that only a
            # marker introduces goes after them, in attribute-name order.
            # `attrValues` alone would sort every name and thus silently
            # reorder a list that a module only wanted to patch.
            listKeys = concatMap (def: if isList def.value then map (e: e.name) def.value else [ ]) defs;
            newKeys = filter (key: !(elem key listKeys)) (attrNames merged);
            order = filter (key: merged ? ${key}) (lib.unique (listKeys ++ newKeys));
          in
          # Put the key back as the `name` of the element. The key wins over an
          # inner `name`. `kubeAttrsToLists` shares this function, so it applies
          # the same rule.
          fromNamedAttrs order merged
        else if anyNumbered then
          fromNumberedAttrs (
            attrsType.merge loc (
              map (
                def:
                def
                // {
                  value = if isList def.value then toNumberedAttrs def.value else stripListMarker def.value;
                }
              ) defs
            )
          )
        else
          listType.merge loc defs;
      nestedTypes.elemType = elemType;
    };

  # A marked attribute set must never type-check as a plain JSON object.
  # `types.oneOf` is a left fold of `either`, and `either` uses the first branch
  # that all definitions pass. Without this guard, `attrsOf` would accept a
  # marked attribute set, because its own check is only `isAttrs`. A lone marked
  # definition would then merge as an object and keep its `_type` marker in the
  # output.
  objectType = types.addCheck (types.attrsOf valueType) (x: !(isNamedList x) && !(isNumberedList x));

  baseType = types.oneOf [
    types.bool
    types.int
    types.float
    types.str
    types.path
    # `namedListOf` comes before `objectType`. A plain JSON object fails its
    # check, so an object still falls through to `objectType`.
    (namedListOf valueType)
    objectType
  ];
  valueType = (types.nullOr baseType) // {
    description = "Kubernetes-shaped JSON value (plain JSON, plus explicit mkNamedList/mkNumberedList override-by-name/index support)";
  };
in
valueType
