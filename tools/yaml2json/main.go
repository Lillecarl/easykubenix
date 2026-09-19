// Command ekn-yaml2json reads a Kubernetes YAML document stream on stdin and
// writes JSON on stdout.
//
// The point is the dialect, not the conversion. Kubernetes YAML is neither
// YAML 1.1 nor YAML 1.2: it is whatever go-yaml v2 does, because that is the
// parser Helm renders through and the API server decodes with. Two scalars
// show the split:
//
//	defaultMode: 0644     an integer, octal, 420 -- YAML 1.1, not 1.2
//	value: 1e+06          a float, 1000000 -- YAML 1.2, not 1.1
//
// Helm emits both, the first from a volume's file mode and the second because
// Go's %v on a float64 goes to scientific notation from 1e6 upwards. No YAML
// version reads both that way, so a parser configured for either version
// gets one of them wrong. This command runs the go-yaml v2 the rest of the
// Kubernetes ecosystem runs, through sigs.k8s.io/yaml, so the dialect is
// inherited rather than described.
//
// Documents are split by k8s.io/apimachinery's YAML reader -- the same
// splitter kubectl uses -- and a document that parses to null is dropped.
// Helm emits those constantly: every template writes a `---` and a
// `# Source:` comment even when its content renders away.
package main

import (
	"bufio"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	utilyaml "k8s.io/apimachinery/pkg/util/yaml"
	sigsyaml "sigs.k8s.io/yaml"
)

// noNamespace is where an object with no metadata.namespace is filed.
//
// It matches easykubenix's `kubernetes.resources`, which uses the same key and
// injects no namespace under it. A rendered template that omits a namespace is
// left alone on purpose: `helm install` does not rewrite the manifest either,
// it lets the API server default it.
const noNamespace = "none"

const crdKind = "CustomResourceDefinition"

// document is one parsed YAML document: its identity, and its JSON verbatim.
//
// The JSON is carried as bytes rather than as a decoded value so that nothing
// this command does can change a number. A decode to `interface{}` turns every
// number into a float64, and re-encoding an int64 past 2^53 then prints a
// different integer.
type document struct {
	index     int
	kind      string
	name      string
	namespace string
	json      json.RawMessage
}

type groupedOutput struct {
	// CustomResourceDefinitions, in stream order.
	//
	// A list and not a grouped map: easykubenix feeds these to
	// `kubernetes.crds`, which takes a list, and reads `spec.names.kind` off
	// each one to teach `kubernetes.apiMappings`.
	CRDs []json.RawMessage `json:"crds"`
	// namespace -> kind -> name -> object.
	Resources map[string]map[string]map[string]json.RawMessage `json:"resources"`
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintf(os.Stderr, "ekn-yaml2json: %v\n", err)
		os.Exit(1)
	}
}

func run() error {
	shape := flag.String("shape", "grouped", `output shape: "grouped" or "list"`)
	crdSplit := flag.Bool("crd-split", true,
		`file CustomResourceDefinitions under "crds" instead of "resources" (--shape=grouped only)`)
	flag.Parse()

	docs, err := readStream(os.Stdin)
	if err != nil {
		return err
	}

	var out []byte
	switch *shape {
	case "list":
		out, err = marshalList(docs)
	case "grouped":
		out, err = marshalGrouped(docs, *crdSplit)
	default:
		return fmt.Errorf("unknown --shape %q, want \"grouped\" or \"list\"", *shape)
	}
	if err != nil {
		return err
	}

	_, err = os.Stdout.Write(append(out, '\n'))
	return err
}

// readStream converts every document in the stream, dropping the empty ones.
func readStream(r io.Reader) ([]document, error) {
	reader := utilyaml.NewYAMLReader(bufio.NewReader(r))

	var docs []document
	for index := 0; ; index++ {
		raw, err := reader.Read()
		if errors.Is(err, io.EOF) {
			return docs, nil
		}
		if err != nil {
			return nil, fmt.Errorf("reading document %d: %w", index, err)
		}

		asJSON, err := sigsyaml.YAMLToJSON(raw)
		if err != nil {
			return nil, fmt.Errorf("document %d: %w", index, err)
		}
		// A comment-only document, an empty one, and a literal `null` all
		// arrive here. Helm produces the first two by the dozen.
		if string(asJSON) == "null" {
			continue
		}

		docs = append(docs, document{index: index, json: asJSON})
	}
}

// identify fills in kind, name and namespace, and rejects a document that is
// not a Kubernetes object.
//
// Only the grouped shape calls this. A list is a faithful copy of the stream
// and has no reason to demand anything of what is in it.
func identify(doc *document) error {
	object := &unstructured.Unstructured{}
	if err := object.UnmarshalJSON(doc.json); err != nil {
		return fmt.Errorf("document %d is not a Kubernetes object: %w", doc.index, err)
	}

	doc.kind = object.GetKind()
	doc.name = object.GetName()
	doc.namespace = object.GetNamespace()

	if doc.name == "" {
		// Grouping is by name, so a nameless object has nowhere to go. The
		// Nix path throws on the same input; this says which document.
		return fmt.Errorf("document %d (%s) has no metadata.name", doc.index, doc.kind)
	}
	if doc.namespace == "" {
		doc.namespace = noNamespace
	}
	return nil
}

func marshalList(docs []document) ([]byte, error) {
	list := make([]json.RawMessage, 0, len(docs))
	for _, doc := range docs {
		list = append(list, doc.json)
	}
	return json.Marshal(list)
}

func marshalGrouped(docs []document, crdSplit bool) ([]byte, error) {
	out := groupedOutput{
		CRDs:      []json.RawMessage{},
		Resources: map[string]map[string]map[string]json.RawMessage{},
	}

	for index := range docs {
		doc := &docs[index]
		if err := identify(doc); err != nil {
			return nil, err
		}

		if crdSplit && doc.kind == crdKind {
			out.CRDs = append(out.CRDs, doc.json)
			continue
		}

		kinds, ok := out.Resources[doc.namespace]
		if !ok {
			kinds = map[string]map[string]json.RawMessage{}
			out.Resources[doc.namespace] = kinds
		}
		names, ok := kinds[doc.kind]
		if !ok {
			names = map[string]json.RawMessage{}
			kinds[doc.kind] = names
		}
		if _, clash := names[doc.name]; clash {
			// Two objects with one identity. The Nix path built this map with
			// `recursiveUpdate`, which deep-merges the pair into an object
			// that was never rendered and applies without complaint. Refusing
			// is the whole reason to do the grouping here.
			return nil, fmt.Errorf(
				"document %d: %s/%s in namespace %s appears twice",
				doc.index, doc.kind, doc.name, doc.namespace)
		}
		names[doc.name] = doc.json
	}

	return json.Marshal(out)
}
