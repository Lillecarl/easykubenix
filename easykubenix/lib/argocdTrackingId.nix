# Build ArgoCD's `argocd.argoproj.io/tracking-id` annotation value for an
# object, as a function suitable for a `gitOps.targets.<name>.annotations`
# entry.
#
# ArgoCD decides whether an object belongs to an Application from a tracking
# label or annotation, selected by `application.resourceTrackingMethod` in
# `argocd-cm`. Since ArgoCD 3.0 the default is `annotation`, and unlike the
# label -- which is just the Application name -- the annotation encodes the
# object's own identity:
#
#     <app name>:<group>/<kind>:<namespace>/<name>
#
# (argo-cd's util/argo/resource_tracking.go, `BuildAppInstanceValue`.)
#
# So it cannot be a constant, which is why this exists: a bootstrap target
# stamping one literal string onto every object would be wrong on all but one
# of them. And wrong here fails *silently* -- ArgoCD treats an annotation that
# does not refer to the object carrying it as belonging to something else
# entirely, so the object neither affects sync status nor is ever pruned. That
# is worse than no annotation at all, and it is exactly the orphaning this is
# meant to prevent.
#
# `namespace` is the Application's `spec.destination.namespace`. It is the
# fallback for cluster-scoped objects, which ArgoCD records under the
# destination namespace rather than under none -- and in easykubenix a
# cluster-scoped object is one declared under the `none` namespace, which is
# the one case that never gets a `metadata.namespace`.
#
# Constructing these by hand is, in ArgoCD's own words, "not an officially
# supported feature" -- their recommendation is to let ArgoCD manage resources
# via GitOps. That is precisely what a bootstrap target cannot do for the
# objects that make GitOps possible in the first place.
{ lib }:
{
  # ArgoCD Application name that should own the object.
  app,
  # The Application's `spec.destination.namespace`.
  namespace,
}:
object:
let
  # `apiVersion` is "group/version", or bare "v1" for the core group -- whose
  # group is the empty string, which is what ArgoCD writes there too.
  parts = lib.splitString "/" object.apiVersion;
  group = if lib.length parts == 1 then "" else lib.head parts;
in
# ArgoCD never puts a tracking annotation on a CRD -- its repo-server skips
# them when it stamps rendered manifests (`!kube.IsCRD(target)` in
# reposerver/repository/repository.go), and `Normalize` skips them again on the
# live side. Stamping one ourselves would therefore be a field ArgoCD's desired
# state never contains while the live object does: a diff on every single sync,
# permanently OutOfSync, on exactly the objects a bootstrap target is most
# likely to be applying. `null` means "no entry", so this declines instead.
#
# `IsCRD` is group+kind, not kind alone, so a CRD *named* CustomResourceDefinition
# in some other group is not one.
if group == "apiextensions.k8s.io" && object.kind == "CustomResourceDefinition" then
  null
else
  "${app}:${group}/${object.kind}:${object.metadata.namespace or namespace}/${object.metadata.name}"
