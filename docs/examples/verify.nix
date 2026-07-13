{
  pkgs,
  lib,
  name,
  manifestJSON,
}:
pkgs.runCommand "check-${name}"
  {
    nativeBuildInputs = with pkgs; [
      jq
      yq-go
    ];
    json = manifestJSON;
    passAsFile = [ "json" ];
  }
  ''
    cd "$TMPDIR"

    # Must be valid JSON
    jq '.' "$jsonPath" > /dev/null || {
      echo "FAIL: manifestJSON is not valid JSON"
      exit 1
    }

    # Must have apiVersion, kind (List), and items
    apiVersion=$(jq -r '.apiVersion' "$jsonPath")
    kind=$(jq -r '.kind' "$jsonPath")
    itemCount=$(jq '.items | length' "$jsonPath")

    echo "apiVersion=$apiVersion kind=$kind items=$itemCount"

    if [[ "$apiVersion" != "v1" ]]; then
      echo "FAIL: expected apiVersion v1, got $apiVersion"
      exit 1
    fi
    if [[ "$kind" != "List" ]]; then
      echo "FAIL: expected kind List, got $kind"
      exit 1
    fi
    if [[ "$itemCount" -eq 0 ]]; then
      echo "FAIL: expected at least 1 item"
      exit 1
    fi

    # Every item must have apiVersion, kind, and metadata
    invalid=$(jq '[.items[] | select(.apiVersion == null or .kind == null or .metadata == null)] | length' "$jsonPath")
    if [[ "$invalid" -gt 0 ]]; then
      echo "FAIL: $invalid item(s) missing apiVersion, kind, or metadata"
      exit 1
    fi

    # Must also be valid YAML when rendered through yq
    yq --prettyPrint '.' "$jsonPath" > manifest.yaml 2>&1 || {
      echo "FAIL: could not render manifest as YAML"
      exit 1
    }

    echo "PASS: ${name}" > "$out"
  ''
