{ lib }:
let
  # The marker uses `_type`, the same tag the module system uses for `mkIf`,
  # `mkMerge` and `mkOverride`. Nixpkgs ignores a `_type` value it does not
  # know, so `dischargeProperties` and `pushDownProperties` pass it through
  # unchanged. See lib/default.nix for the same argument about the two list
  # markers.
  markerType = "ifExists";

  isIfExists = value: lib.isAttrs value && (value._type or null) == markerType;

  # Contribute `content` to this key only if the key already exists.
  #
  # "Exists" means one ordinary definition for that exact key survives the
  # normal module conditions. An `mkIf false` definition is not one. A
  # definition made only by other `mkIfExists` markers is not one either.
  #
  # Put a priority inside the content, not around the marker. Write
  # `mkIfExists { replicas = lib.mkForce 3; }`, never
  # `lib.mkForce (mkIfExists { ... })`.
  mkIfExists = content: {
    _type = markerType;
    inherit content;
  };

  # The path form. `mkIfExistsAtPath "namespace.Kind.name" value` expands into
  # a conditional definition at every attribute boundary, so the namespace, the
  # Kind and the object must each already exist.
  #
  # The list form `mkIfExistsAtPath [ "namespace" "Kind" "name" ] value` is the
  # lossless one. Use it for a key that contains a dot.
  #
  # A string path is split on ".". It is not parsed. Quotes and backslashes are
  # rejected rather than given a meaning, because a path parser here would be a
  # second, worse copy of the module system's own attribute-path rules.
  mkIfExistsAtPath =
    path: content:
    let
      normalizedPath =
        if lib.isList path then
          path
        else if lib.isString path then
          assert lib.assertMsg (
            !lib.any (character: lib.hasInfix character path) [
              "\""
              "'"
              "\\"
            ]
          ) "mkIfExistsAtPath string paths do not support quoting or escaping; use a list path instead";
          lib.splitString "." path
        else
          throw "mkIfExistsAtPath requires an attribute path list or dot-separated string";
    in
    assert lib.assertMsg (
      normalizedPath != [ ] && lib.all (name: lib.isString name && name != "") normalizedPath
    ) "mkIfExistsAtPath requires a non-empty attribute path of non-empty strings";
    lib.foldr (name: nested: { ${name} = mkIfExists nested; }) content normalizedPath;

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

  # Classify a key's definitions only after the module system has processed its
  # own properties. This type never inspects a raw `mkIf` itself. It hands the
  # definitions to `mergeDefinitions` with a type that merges to the definition
  # list, and reads the result.
  #
  # Two things follow. An ordinary `mkIf false` definition counts as absent,
  # which is what a reader expects. And each surviving definition keeps its
  # source file, so the real element merge still reports a useful position.
  definitionCollector = lib.types.mkOptionType {
    name = "conditionalAttrsOf definitions";
    description = "definitions collected for conditionalAttrsOf";
    check = {
      __functor = _: _: true;
      isV2MergeCoherent = true;
    };
    merge = rec {
      __functor =
        self: loc: defs:
        (self.v2 { inherit loc defs; }).value;
      v2 =
        { defs, ... }:
        {
          headError = null;
          value = defs;
          valueMeta = { };
        };
    };
  };

  # `attrsOf`, plus the `mkIfExists` marker.
  #
  # A key exists when an ordinary definition for it survives. A key that only
  # `mkIfExists` markers define does not exist, and this type drops it. A key
  # that does exist merges the marker content together with the ordinary
  # definitions, by the normal rules.
  #
  # The purpose is a patch that is safe to write against a resource set you do
  # not own. A conditional namespace can add children to a namespace another
  # module made. It cannot create the namespace itself. The same holds at every
  # level, so an environment can say "if this chart still ships that Deployment,
  # give it three replicas" without inventing the Deployment when the chart
  # drops it.
  conditionalAttrsWith =
    { elemType }:
    let
      # Push the position of each definition down onto each of its keys, the
      # way nixpkgs' own `attrsWith` does.
      pushPositions = map (
        definition:
        lib.mapAttrs (_name: value: {
          inherit (definition) file;
          inherit value;
        }) definition.value
      );

      mergeEntry =
        loc: definitions:
        let
          # The fast path. No definition for this key carries any `_type`, so
          # no marker and no module property can be hiding in one. Merge the
          # definitions directly, exactly as `attrsOf` would.
          plainOrdinary = lib.all (
            definition: !(lib.isAttrs definition.value && definition.value ? _type)
          ) definitions;
          collected =
            (lib.modules.mergeDefinitions loc definitionCollector definitions).optionalValue.value or [ ];
          ordinary = lib.filter (definition: !isIfExists definition.value) collected;
          conditional = lib.filter (definition: isIfExists definition.value) collected;
          included =
            ordinary ++ map (definition: definition // { value = definition.value.content; }) conditional;
          evaluation = lib.modules.mergeDefinitions loc elemType (
            if plainOrdinary then definitions else included
          );
        in
        {
          exists = plainOrdinary || ordinary != [ ];
          value = evaluation.mergedValue;
          valueMeta = evaluation.checkedAndMerged.valueMeta;
        };
    in
    lib.types.mkOptionType rec {
      name = "conditionalAttrsOf";
      description = "attribute set of ${elemType.description} with mkIfExists support";
      descriptionClass = "composite";
      # A marker is not a value of this type. Rejecting it here stops a lone
      # marker at the top of an option from merging as an ordinary attribute
      # set and keeping its `_type` in the output.
      check = {
        __functor = _: value: lib.isAttrs value && !isIfExists value;
        isV2MergeCoherent = true;
      };
      merge = rec {
        __functor =
          self: loc: defs:
          (self.v2 { inherit loc defs; }).value;
        v2 =
          { loc, defs }:
          let
            entries = lib.zipAttrsWith (name: entryDefinitions: mergeEntry (loc ++ [ name ]) entryDefinitions) (
              pushPositions defs
            );
            includedEntries = lib.filterAttrs (_: entry: entry.exists) entries;
          in
          {
            headError = checkDefsForError check defs;
            value = lib.mapAttrs (_: entry: entry.value) includedEntries;
            # Only a key that survived the existence filter has metadata. A
            # reader of `valueMeta.attrs` sees the same key set as the value.
            valueMeta.attrs = lib.mapAttrs (_: entry: entry.valueMeta) includedEntries;
          };
      };
      emptyValue.value = { };
      getSubOptions = prefix: elemType.getSubOptions (prefix ++ [ "<name>" ]);
      getSubModules = elemType.getSubModules;
      substSubModules = m: conditionalAttrsWith { elemType = elemType.substSubModules m; };
      functor = {
        inherit name;
        type = conditionalAttrsWith;
        payload = { inherit elemType; };
        binOp =
          a: b:
          let
            merged = a.elemType.typeMerge b.elemType.functor;
          in
          if merged == null then null else { elemType = merged; };
      };
      nestedTypes.elemType = elemType;
    };

  conditionalAttrsOf = elemType: conditionalAttrsWith { inherit elemType; };
in
{
  inherit
    conditionalAttrsOf
    isIfExists
    mkIfExists
    mkIfExistsAtPath
    ;
}
