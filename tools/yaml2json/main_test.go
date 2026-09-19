package main

import (
	"encoding/json"
	"strings"
	"testing"
)

// The scalar table is the specification. Every row here is a scalar some
// chart in this project actually renders, and each one is read differently by
// at least two of {YAML 1.1, YAML 1.2, go-yaml v2}. They are asserted against
// the JSON text rather than a decoded value so that the int/float distinction
// is visible.
func TestScalarDialect(t *testing.T) {
	for _, row := range []struct {
		name string
		yaml string
		want string
	}{
		{
			// YAML 1.1 octal. A volume's file mode: 0644 means 420, the Unix
			// convention. YAML 1.2 reads the same text as decimal 644.
			name: "leading zero is octal",
			yaml: "defaultMode: 0644",
			want: `{"defaultMode":420}`,
		},
		{
			// YAML 1.2 float. Helm renders a chart's `priorityClass.value:
			// 1000000` through Go's %v on a float64, which prints 1e+06.
			// YAML 1.1 has no float production for it and reads a string,
			// which the API server then refuses.
			name: "bare exponent is a float",
			yaml: "value: 1e+06",
			want: `{"value":1000000}`,
		},
		{
			// YAML 1.1 booleans. prometheus-operator and friends emit these.
			name: "yes and on are booleans",
			yaml: "a: yes\nb: on\nc: off\nd: no",
			want: `{"a":true,"b":true,"c":false,"d":false}`,
		},
		{
			// A bare `=` is YAML 1.1's "value" tag. Charts use it as a
			// literal string -- prometheus-operator's Alertmanager CRD
			// enumerates it as a matcher operator.
			name: "bare equals is a string",
			yaml: "operator: =",
			want: `{"operator":"="}`,
		},
		{
			name: "explicit octal",
			yaml: "mode: 0o755",
			want: `{"mode":493}`,
		},
		{
			name: "hex",
			yaml: "mask: 0xff",
			want: `{"mask":255}`,
		},
		{
			// A non-string key. JSON has only string keys, so the converter
			// has to do something; this records what.
			name: "boolean key",
			yaml: "yes: 1",
			want: `{"true":1}`,
		},
		{
			// Past 2^53 a float64 round trip prints a different integer.
			// Carrying the JSON as bytes is what keeps this exact.
			name: "large integer keeps every digit",
			yaml: "generation: 9007199254740993",
			want: `{"generation":9007199254740993}`,
		},
		{
			name: "quoted stays a string",
			yaml: `port: "8080"`,
			want: `{"port":"8080"}`,
		},
	} {
		t.Run(row.name, func(t *testing.T) {
			docs, err := readStream(strings.NewReader(row.yaml))
			if err != nil {
				t.Fatalf("readStream: %v", err)
			}
			if len(docs) != 1 {
				t.Fatalf("got %d documents, want 1", len(docs))
			}
			if got := string(docs[0].json); got != row.want {
				t.Errorf("got %s, want %s", got, row.want)
			}
		})
	}
}

// YAML has infinity and NaN; JSON has neither. This is the one scalar the
// dialect can read and this command cannot write, so it fails rather than
// inventing a value. Measured: the conversion itself refuses, with
// "json: unsupported value: +Inf", and the document index says where.
func TestInfinityIsRejected(t *testing.T) {
	for _, source := range []string{"limit: .inf", "limit: -.inf", "limit: .nan"} {
		_, err := readStream(strings.NewReader(source))
		if err == nil {
			t.Errorf("%q converted; want an error", source)
			continue
		}
		if !strings.Contains(err.Error(), "document 0") {
			t.Errorf("%q: got %q, want it to name the document", source, err)
		}
	}
}

// Helm writes a `---` and a `# Source:` comment for every template, including
// the ones whose content renders away entirely.
func TestEmptyDocumentsAreDropped(t *testing.T) {
	stream := `---
# Source: chart/templates/ignored.yaml
---
# Source: chart/templates/service.yaml
apiVersion: v1
kind: Service
metadata:
  name: web
---
`
	docs, err := readStream(strings.NewReader(stream))
	if err != nil {
		t.Fatalf("readStream: %v", err)
	}
	if len(docs) != 1 {
		t.Fatalf("got %d documents, want 1", len(docs))
	}
}

func TestListKeepsOrderAndNeedsNoObject(t *testing.T) {
	stream := "- 1\n- 2\n---\nplain string\n---\n{\"kind\":\"Service\"}\n"
	docs, err := readStream(strings.NewReader(stream))
	if err != nil {
		t.Fatalf("readStream: %v", err)
	}
	out, err := marshalList(docs)
	if err != nil {
		t.Fatalf("marshalList: %v", err)
	}
	want := `[[1,2],"plain string",{"kind":"Service"}]`
	if string(out) != want {
		t.Errorf("got %s, want %s", out, want)
	}
}

const grouped = `
apiVersion: v1
kind: Service
metadata:
  name: web
  namespace: shop
---
apiVersion: v1
kind: Namespace
metadata:
  name: shop
---
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: widgets.example.com
`

func TestGrouping(t *testing.T) {
	docs, err := readStream(strings.NewReader(grouped))
	if err != nil {
		t.Fatalf("readStream: %v", err)
	}
	out, err := marshalGrouped(docs, true)
	if err != nil {
		t.Fatalf("marshalGrouped: %v", err)
	}

	var got groupedOutput
	if err := json.Unmarshal(out, &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}

	if len(got.CRDs) != 1 {
		t.Fatalf("got %d CRDs, want 1", len(got.CRDs))
	}
	if _, ok := got.Resources["shop"]["Service"]["web"]; !ok {
		t.Errorf("Service/web is not under namespace shop: %s", out)
	}
	// A Namespace object carries no metadata.namespace of its own, so it is
	// cluster-scoped and files under "none" -- the same key easykubenix's
	// `kubernetes.resources` uses.
	if _, ok := got.Resources[noNamespace]["Namespace"]["shop"]; !ok {
		t.Errorf("Namespace/shop is not under %q: %s", noNamespace, out)
	}
	if _, ok := got.Resources["shop"]["CustomResourceDefinition"]; ok {
		t.Errorf("the CRD was also filed under resources: %s", out)
	}
}

func TestCRDSplitOff(t *testing.T) {
	docs, err := readStream(strings.NewReader(grouped))
	if err != nil {
		t.Fatalf("readStream: %v", err)
	}
	out, err := marshalGrouped(docs, false)
	if err != nil {
		t.Fatalf("marshalGrouped: %v", err)
	}

	var got groupedOutput
	if err := json.Unmarshal(out, &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if len(got.CRDs) != 0 {
		t.Errorf("got %d CRDs, want 0", len(got.CRDs))
	}
	if _, ok := got.Resources[noNamespace]["CustomResourceDefinition"]["widgets.example.com"]; !ok {
		t.Errorf("the CRD is not under resources: %s", out)
	}
}

func TestGroupingRejects(t *testing.T) {
	for _, row := range []struct {
		name string
		yaml string
		want string
	}{
		{
			name: "no name",
			yaml: "apiVersion: v1\nkind: Service\nmetadata: {}\n",
			want: "no metadata.name",
		},
		{
			// `recursiveUpdate` in the Nix path deep-merges these into an
			// object nothing rendered, and it applies.
			name: "same identity twice",
			yaml: "apiVersion: v1\nkind: Service\nmetadata:\n  name: web\n" +
				"---\napiVersion: v1\nkind: Service\nmetadata:\n  name: web\n",
			want: "appears twice",
		},
		{
			name: "not an object",
			yaml: "- 1\n- 2\n",
			want: "not a Kubernetes object",
		},
	} {
		t.Run(row.name, func(t *testing.T) {
			docs, err := readStream(strings.NewReader(row.yaml))
			if err != nil {
				t.Fatalf("readStream: %v", err)
			}
			_, err = marshalGrouped(docs, true)
			if err == nil {
				t.Fatalf("want an error mentioning %q, got none", row.want)
			}
			if !strings.Contains(err.Error(), row.want) {
				t.Errorf("got %q, want it to mention %q", err, row.want)
			}
		})
	}
}

func TestEmptyStreamIsAnEmptyResult(t *testing.T) {
	docs, err := readStream(strings.NewReader(""))
	if err != nil {
		t.Fatalf("readStream: %v", err)
	}
	list, err := marshalList(docs)
	if err != nil {
		t.Fatalf("marshalList: %v", err)
	}
	if string(list) != "[]" {
		t.Errorf("got %s, want []", list)
	}
	group, err := marshalGrouped(docs, true)
	if err != nil {
		t.Fatalf("marshalGrouped: %v", err)
	}
	if string(group) != `{"crds":[],"resources":{}}` {
		t.Errorf("got %s", group)
	}
}
