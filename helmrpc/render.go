package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"sort"
	"strings"

	"sigs.k8s.io/yaml"

	"helm.sh/helm/v4/pkg/action"
	"helm.sh/helm/v4/pkg/chart/common"
	"helm.sh/helm/v4/pkg/chart/loader"
	kubefake "helm.sh/helm/v4/pkg/kube/fake"
	ri "helm.sh/helm/v4/pkg/release"
	release "helm.sh/helm/v4/pkg/release/v1"
	releaseutil "helm.sh/helm/v4/pkg/release/v1/util"
	"helm.sh/helm/v4/pkg/storage"
	"helm.sh/helm/v4/pkg/storage/driver"

	"github.com/lillecarl/easykubenix/helmrpc/pb"
)

// renderRequestBody is RenderRequest.request_json decoded. Field names match
// the Nix-facing attrset builtins.renderHelm takes (see easykubenix's
// ekn/src/ekn/helm.py) one for one, so neither side needs a translation
// layer between what Nix passes and what crosses the wire.
type renderRequestBody struct {
	Chart       string         `json:"chart"`
	Name        string         `json:"name"`
	Namespace   string         `json:"namespace"`
	Values      map[string]any `json:"values"`
	KubeVersion string         `json:"kubeVersion"`
	IncludeCRDs bool           `json:"includeCRDs"`
	NoHooks     bool           `json:"noHooks"`
	APIVersions []string       `json:"apiVersions"`
}

// renderResponseBody is RenderResponse.response_json decoded. Each resource
// is a plain JSON object, not a nested google.protobuf.Struct -- see the
// .proto file for why.
type renderResponseBody struct {
	Resources []map[string]any `json:"resources"`
}

// renderChart runs the same client-side dry-run install `helm template` uses
// (see helm.sh/helm/v4/pkg/cmd/template.go), so callers get exactly `helm
// template`'s rendering semantics without shelling out to the CLI or
// round-tripping the output through a second YAML parser.
func renderChart(ctx context.Context, req *pb.RenderRequest) (*pb.RenderResponse, error) {
	var body renderRequestBody
	if err := json.Unmarshal(req.GetRequestJson(), &body); err != nil {
		return nil, fmt.Errorf("decoding request_json: %w", err)
	}

	chrt, err := loader.Load(body.Chart)
	if err != nil {
		return nil, fmt.Errorf("loading chart: %w", err)
	}

	cfg := &action.Configuration{
		Releases: storage.Init(driver.NewMemory()),
		// PrintingKubeClient never touches a real cluster; DryRunClient below
		// also means Helm never calls into it, but Configuration requires a
		// non-nil KubeClient regardless.
		KubeClient:   &kubefake.PrintingKubeClient{Out: io.Discard},
		Capabilities: common.DefaultCapabilities,
	}

	client := action.NewInstall(cfg)
	client.DryRunStrategy = action.DryRunClient
	client.Replace = true
	client.ReleaseName = body.Name
	if client.ReleaseName == "" {
		client.ReleaseName = "release-name"
	}
	client.Namespace = body.Namespace
	client.IncludeCRDs = body.IncludeCRDs
	client.DisableHooks = body.NoHooks
	client.APIVersions = common.VersionSet(body.APIVersions)

	if body.KubeVersion != "" {
		parsed, err := common.ParseKubeVersion(body.KubeVersion)
		if err != nil {
			return nil, fmt.Errorf("invalid kube version %q: %w", body.KubeVersion, err)
		}
		client.KubeVersion = parsed
	}

	vals := body.Values
	if vals == nil {
		vals = map[string]any{}
	}

	relI, err := client.RunWithContext(ctx, chrt, vals)
	if err != nil {
		return nil, fmt.Errorf("rendering chart: %w", err)
	}
	rel, err := releaserToV1Release(relI)
	if err != nil {
		return nil, err
	}

	var manifests bytes.Buffer
	fmt.Fprintln(&manifests, strings.TrimSpace(rel.Manifest))
	if !client.DisableHooks {
		for _, hook := range rel.Hooks {
			fmt.Fprintf(&manifests, "---\n# Source: %s\n%s\n", hook.Path, hook.Manifest)
		}
	}

	resources, err := structuredResources(manifests.String())
	if err != nil {
		return nil, err
	}

	encoded, err := json.Marshal(renderResponseBody{Resources: resources})
	if err != nil {
		return nil, fmt.Errorf("encoding response_json: %w", err)
	}

	return &pb.RenderResponse{ResponseJson: encoded}, nil
}

// releaserToV1Release mirrors the small unexported helper of the same name
// duplicated across several helm.sh/helm/v4 packages (e.g.
// pkg/action/get_values.go) -- Release.Run returns the release.Releaser
// interface, and callers outside those packages have no exported way to get
// the concrete *release/v1.Release back out of it.
func releaserToV1Release(rel ri.Releaser) (*release.Release, error) {
	switch r := rel.(type) {
	case release.Release:
		return &r, nil
	case *release.Release:
		return r, nil
	default:
		return nil, fmt.Errorf("unsupported release type: %T", rel)
	}
}

// structuredResources splits a multi-document YAML manifest the way `helm
// template` renders it and decodes each document with sigs.k8s.io/yaml (the
// same YAML 1.1-compatible decoder Helm itself uses, so values come out
// exactly as Helm rendered them). The resulting maps are JSON-marshaled once,
// together, as part of the overall response body -- see the .proto file for
// why this isn't google.protobuf.Struct.
func structuredResources(bigFile string) ([]map[string]any, error) {
	split := releaseutil.SplitManifests(bigFile)
	keys := make([]string, 0, len(split))
	for k := range split {
		keys = append(keys, k)
	}
	sort.Sort(releaseutil.BySplitManifestsOrder(keys))

	resources := make([]map[string]any, 0, len(keys))
	for _, k := range keys {
		doc := strings.TrimSpace(split[k])
		if doc == "" {
			continue
		}

		var obj map[string]any
		if err := yaml.Unmarshal([]byte(doc), &obj); err != nil {
			return nil, fmt.Errorf("decoding rendered manifest %q: %w", k, err)
		}
		if len(obj) == 0 {
			continue
		}

		resources = append(resources, obj)
	}
	return resources, nil
}
