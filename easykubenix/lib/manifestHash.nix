{ lib, isSeededObject }:
let
  annotation = "ekn.dev/manifest-hash";

  /**
    The object as it is hashed: itself, without the hash annotation.

    An object that carries no such annotation is left completely alone. An
    empty `annotations = { }` is not the same JSON as no `annotations` key,
    so removing the key only when it is there keeps restamping idempotent.

    Only that one key. `ekn.dev/environment` deliberately stays in whatever it
    was, because the render never writes it: `ekn` stamps it at apply time. So
    an object a GitOps engine synced from the committed YAML hashes the same
    as one `ekn` applied, which is the whole property this annotation exists
    for.
  */
  stripHash =
    object:
    let
      annotations = object.metadata.annotations or { };
      kept = removeAttrs annotations [ annotation ];
    in
    if !(annotations ? ${annotation}) then
      object
    else
      object
      // {
        metadata =
          if kept == { } then
            removeAttrs object.metadata [ "annotations" ]
          else
            object.metadata // { annotations = kept; };
      };
in
rec {
  manifestHashAnnotation = annotation;

  /**
    The `sha256:<hex>` an object should carry.

    `builtins.toJSON` is the canonicalisation: sorted keys, no spaces, no
    trailing newline.

    This is the only producer. `ekn` reads the value and never recomputes it.
    A `kubernetes.rawFiles` entry is never parsed here, so it carries no hash,
    and `ekn kubeapply` applies it on every run.
  */
  manifestHash =
    object: "sha256:" + builtins.hashString "sha256" (builtins.toJSON (stripHash object));

  /**
    The object, carrying the hash of itself.

    **Not stamped on an object that holds a credential.** A SOPS-encrypted
    object and a seeded one both carry a value that is not in git while the
    rest of the manifest is, so a digest published in the manifest is a
    brute-force oracle for that one field. SOPS also re-encrypts with a fresh
    MAC, so the value would churn on every render anyway.

    Idempotent: hashing strips any annotation already there, so an object that
    is stamped, re-labelled and stamped again carries the hash of what it is
    now rather than of what it was.
  */
  stampManifestHash =
    object:
    if lib.isAttrs (object.sops or null) || isSeededObject object then
      object
    else
      lib.recursiveUpdate object { metadata.annotations.${annotation} = manifestHash object; };
}
