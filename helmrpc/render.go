package main

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"sort"
	"strings"

	"google.golang.org/protobuf/types/known/structpb"
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

// renderChart runs the same client-side dry-run install `helm template` uses
// (see helm.sh/helm/v4/pkg/cmd/template.go), so callers get exactly `helm
// template`'s rendering semantics without shelling out to the CLI or
// round-tripping the output through a second YAML parser.
func renderChart(ctx context.Context, req *pb.RenderRequest) (*pb.RenderResponse, error) {
	chrt, err := loader.Load(req.GetChartPath())
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
	client.ReleaseName = req.GetName()
	if client.ReleaseName == "" {
		client.ReleaseName = "release-name"
	}
	client.Namespace = req.GetNamespace()
	client.IncludeCRDs = req.GetIncludeCrds()
	client.DisableHooks = req.GetNoHooks()
	client.APIVersions = common.VersionSet(req.GetApiVersions())

	if kubeVersion := req.GetKubeVersion(); kubeVersion != "" {
		parsed, err := common.ParseKubeVersion(kubeVersion)
		if err != nil {
			return nil, fmt.Errorf("invalid kube version %q: %w", kubeVersion, err)
		}
		client.KubeVersion = parsed
	}

	vals := map[string]any{}
	if v := req.GetValues(); v != nil {
		vals = v.AsMap()
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

	return &pb.RenderResponse{Resources: resources}, nil
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
// template` renders it, decodes each document with sigs.k8s.io/yaml (the
// same YAML 1.1-compatible decoder Helm itself uses, so values come out
// exactly as Helm rendered them), and hands the resulting maps to protobuf's
// structpb directly -- no second, independent YAML parser touches the data.
func structuredResources(bigFile string) ([]*structpb.Struct, error) {
	split := releaseutil.SplitManifests(bigFile)
	keys := make([]string, 0, len(split))
	for k := range split {
		keys = append(keys, k)
	}
	sort.Sort(releaseutil.BySplitManifestsOrder(keys))

	resources := make([]*structpb.Struct, 0, len(keys))
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

		s, err := structpb.NewStruct(obj)
		if err != nil {
			return nil, fmt.Errorf("converting manifest %q to structpb: %w", k, err)
		}
		resources = append(resources, s)
	}
	return resources, nil
}
