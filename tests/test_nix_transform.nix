let
  sources = import ../nix/sources.nix;
  pkgs = import sources.nixpkgs { };
  # The same `lib.extend` the real module evaluation uses, so a transform
  # here reaches the same overlay a module author reaches.
  lib = pkgs.lib.extend (import ../easykubenix/lib);
  nixTransform = import ../easykubenix/lib/nixTransform.nix {
    inherit lib pkgs;
    inherit (sources) nixpkgs;
  };

  dashboard = builtins.toFile "dashboard.json" (
    builtins.toJSON {
      title = "Cilium Metrics";
      panels = [
        { id = 1; }
        { id = 2; }
        { id = 3; }
      ];
    }
  );
in
{
  # A transform that walks the input, which is what a data-only override
  # cannot express.
  walksTheInput = nixTransform {
    name = "dashboard";
    src = dashboard;
    transformer = ''
      { lib, value }:
      value
      // {
        title = "patched: " + (value.title or "untitled");
        panelCount = builtins.length (value.panels or [ ]);
      }
    '';
  };

  # easykubenix's own overlay is in scope inside the sandbox.
  reachesTheEknOverlay = nixTransform {
    name = "overlay";
    src = dashboard;
    transformer = ''
      { lib, value }:
      { hash = lib.hashAttrs { a = 1; }; }
    '';
  };

  # `mkNamedList` leaves a `_type` marker. The result goes into an untyped
  # option, which walks nothing, so the runner must convert it here or the
  # marker reaches the cluster as a literal field.
  convertsANamedList = nixTransform {
    name = "named";
    src = dashboard;
    transformer = ''
      { lib, value }:
      {
        containers = lib.mkNamedList {
          app = { image = "v1"; };
          sidecar = { image = "s1"; };
        };
      }
    '';
  };

  convertsANumberedList = nixTransform {
    name = "numbered";
    src = dashboard;
    transformer = ''
      { lib, value }:
      {
        args = lib.mkNumberedList {
          "1" = "--second";
          "0" = "--first";
        };
      }
    '';
  };

  # A marker with no option to merge against cannot be converted, so the
  # runner refuses rather than emitting `_type`.
  refusesAnUnconvertibleMarker = nixTransform {
    name = "unconvertible";
    src = dashboard;
    transformer = ''
      { lib, value }:
      { spec = lib.mkIfExists { replicas = 3; }; }
    '';
  };

  # The transform reads a file, so a JSON input of any size costs the render
  # one `readFile` regardless of what the transform does to it.
  isJustAReadFileAfterwards = builtins.isAttrs (nixTransform {
    name = "cached";
    src = dashboard;
    transformer = ''
      { lib, value }:
      value
    '';
  });
}
