{ lib }:
let
  inherit (lib)
    mkOptionType
    types
    isList
    isAttrs
    any
    listToAttrs
    attrValues
    removeAttrs
    ;

  isNamedListMarked = value: isAttrs value && (value._namedlist or false) == true;
  isNumberedListMarked = value: isAttrs value && (value._numberedlist or false) == true;

  toNamedAttrs =
    list:
    listToAttrs (
      map (e: {
        inherit (e) name;
        value = e;
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
  fromNumberedAttrs =
    attrs:
    lib.pipe attrs [
      (a: removeAttrs a [ "_numberedlist" ])
      lib.attrsToList
      (lib.sort (a: b: (lib.toInt a.name) < (lib.toInt b.name)))
      (map (x: x.value))
    ];

  # A `listOf elemType` alternative that also accepts the *explicit*
  # `lib.mkNamedList`/`lib.mkNumberedList` marker conventions (an attrset
  # tagged `_namedlist`/`_numberedlist` = true) as an override-by-name (or
  # order-preserving override-by-index) shorthand.
  #
  # This is deliberately opt-in, not heuristic: Kubernetes fields have one
  # fixed shape each -- always a list, never interchangeable with a map --
  # so an ordinary, UNMARKED attrset must never be silently reinterpreted
  # as "the attrs form of a list" just because its values happen to look
  # list-element-shaped (an earlier version of this type guessed from
  # structure alone -- e.g. treating any list of attrsets with a `name`
  # field as override-by-name-eligible -- which silently misparsed
  # `metadata.ownerReferences` entries that legitimately share a `name`
  # across different owner `kind`s, dropping one, and would have equally
  # misparsed an accidental single-object-instead-of-list typo as a valid
  # named list). Only a module that deliberately calls
  # `lib.mkNamedList`/`lib.mkNumberedList` opts a value into this merge
  # behavior; every other list, marked or not, behaves exactly like plain
  # `listOf`.
  #
  # Reconciling a plain list definition (e.g. straight from a rendered Helm
  # chart, never pre-converted) against a later *marked* override is what
  # lets by-name overrides work without every producer (helm.nix,
  # importyaml.nix, hand-authored modules, ...) having to agree on
  # pre-converting its own output first.
  namedListOf =
    elemType:
    let
      attrsType = types.attrsOf elemType;
      listType = types.listOf elemType;
    in
    mkOptionType {
      name = "namedListOf";
      description = "list of ${elemType.description}, or an mkNamedList/mkNumberedList-tagged attrset";
      check = x: listType.check x || isNamedListMarked x || isNumberedListMarked x;
      merge =
        loc: defs:
        let
          anyNamed = any (def: isNamedListMarked def.value) defs;
          anyNumbered = any (def: isNumberedListMarked def.value) defs;
        in
        if anyNamed then
          attrValues (
            attrsType.merge loc (
              map (
                def:
                def
                // {
                  value = if isList def.value then toNamedAttrs def.value else removeAttrs def.value [ "_namedlist" ];
                }
              ) defs
            )
          )
        else if anyNumbered then
          fromNumberedAttrs (
            attrsType.merge loc (
              map (
                def:
                def
                // {
                  value = if isList def.value then toNumberedAttrs def.value else def.value;
                }
              ) defs
            )
          )
        else
          listType.merge loc defs;
    };

  baseType = types.oneOf [
    types.bool
    types.int
    types.float
    types.str
    types.path
    (types.attrsOf valueType)
    (namedListOf valueType)
  ];
  valueType = (types.nullOr baseType) // {
    description = "Kubernetes-shaped JSON value (plain JSON, plus explicit mkNamedList/mkNumberedList override-by-name/index support)";
  };
in
valueType
