self: lib: {
  # Takes an attribute set and marks it as a `_namedlist`.
  # It validates that every value within the input attrset is also an attrset.
  # This is intended for manually defining `_namedlist` structures in Nix code.
  mkNamedList =
    attrs:
    if !lib.isAttrs attrs then
      throw "mkNamedList error: Input must be an attribute set."
    # Ensure all children are also attribute sets, as required by the `_namedlist` contract.
    else if !(lib.all lib.isAttrs (lib.attrValues attrs)) then
      throw "mkNamedList error: All values in the attribute set must themselves be attribute sets."
    else
      attrs // { _namedlist = true; };

  # Takes an attribute set and marks it as a `_numberedlist`.
  # It validates that all keys are valid integer strings (e.g., "0", "5", "100").
  # This ensures the structure can be losslessly converted back to an ordered list by sorting the keys numerically.
  mkNumberedList =
    attrs:
    if !lib.isAttrs attrs then
      throw "mkNumberedList error: Input must be an attribute set."
    else if !(lib.all lib.isAttrs (lib.attrValues attrs)) then
      throw "mkNumberedList error: All values in the attribute set must themselves be attribute sets."
    else
      let
        keys = lib.attrNames attrs;
        # Check if every key can be successfully parsed as an integer.
        allKeysAreInts = lib.all (key: (builtins.tryEval (lib.toIntBase10 key)).success) keys;
      in
      if !allKeysAreInts then
        throw "mkNumberedList error: All keys in the attribute set must be integer strings."
      else
        attrs // { _numberedlist = true; };

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

  # Master transformer: Converts special `_namedlist` and `_numberedlist` attribute sets
  # back into standard JSON lists. This function is the inverse of `kubeListsToAttrs`.
  kubeAttrsToLists =
    path: value:
    if lib.isAttrs value then
      if value._namedlist or false == true then
        # `_namedlist`s are attribute sets where keys were derived from a `name` attribute.
        # Convert back to a list of attribute sets, injecting the key back as the `name` attribute.
        lib.mapAttrsToList (
          name: val:
          if !lib.isAttrs val then
            throw "namedListToList error: Value for key '${name}' is not an attribute set."
          else
            val // { inherit name; }
        ) (lib.removeAttrs value [ "_namedlist" ])
      else if value._numberedlist or false == true then
        # `_numberedlist`s are attribute sets where keys are numeric indices ("0", "1", ...).
        # Convert back to a simple list of values, ensuring order is preserved by sorting the keys.
        lib.pipe value [
          (x: lib.removeAttrs x [ "_numberedlist" ])
          lib.attrsToList
          (lib.sort (a: b: (lib.toInt a.name) < (lib.toInt b.name)))
          (map (x: x.value))
        ]
      else
        value
    else
      value;

  # Master transformer: Converts all standard JSON lists into special attribute sets
  # to make them easily overridable in Nix.
  # Takes the root object first to allow skipping entire objects (e.g., CRDs).
  kubeListsToAttrs =
    object:
    let
      # Never transform CustomResourceDefinition objects. Their spec contains OpenAPI schemas
      # with deeply nested structures that must not be converted to named/numbered lists.
      isCRD = lib.isAttrs object && (object.kind or null) == "CustomResourceDefinition";
    in
    path: value:
    if isCRD then
      value
    else
    let
      currentKey = lib.last (path ++ [ null ]);
      # Heuristic to identify a list that should become a `_namedlist`.
      # A list is a candidate if all its elements are attribute sets that contain a `name` key.
      isNamedListCandidate =
        lib.isList value
        && (lib.all (v: lib.isAttrs v && lib.hasAttr "name" v) value)
        # Special exclusion for Kubernetes `initContainers`. The order of init containers is
        # significant and must be preserved. By failing this check, it will be converted
        # to a `_numberedlist` instead, which preserves order.
        && currentKey != "initContainers";
    in
    if isNamedListCandidate then
      # Convert the list to a `_namedlist` attribute set. The `name` attribute of each
      # element becomes the key in the resulting attribute set.
      lib.pipe value [
        (lib.map (x: {
          inherit (x) name;
          value = lib.removeAttrs x [ "name" ];
        }))
        lib.listToAttrs
        (x: x // { _namedlist = true; })
      ]
    # Any other list (e.g., simple string lists like container `args`, or `initContainers`)
    # is converted to a `_numberedlist`.
    else if lib.isList value then
      # The list index becomes the key (e.g., "0", "1", "2", ...), preserving order.
      lib.pipe value [
        (lib.imap0 (
          i: v: {
            name = toString i;
            value = v;
          }
        ))
        lib.listToAttrs
        (x: x // { _numberedlist = true; })
      ]
    else
      value;

  # md5 hash an attrset, useful to trigger rollouts by hashing ConfigMaps.
  hashAttrs = attrs: builtins.hashString "md5" (builtins.toJSON attrs);
}
