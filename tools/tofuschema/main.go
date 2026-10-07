// ekn-tofuschema writes OpenTofu's core schema as JSON.
//
// The core schema is every block OpenTofu knows without a provider:
// `terraform`, `variable`, `output`, `module`, and the meta-arguments of
// `resource` and `data`. It exists only as Go code in opentofu-schema, the
// library the OpenTofu language server reads, so this program is the one
// part of the tofu schema check that is not Python. `ekn.tofuschema` merges
// the providers' schemas over it and translates the result into JSON Schema.
//
// Constraints keep their hcl-lang kind and carry types as cty's own JSON, so
// nothing here decides what a value may look like. An unknown constraint kind
// is an error: a new kind needs a rule in ekn, not a silent default.
package main

import (
	"encoding/json"
	"fmt"
	"os"

	"github.com/hashicorp/go-version"
	"github.com/hashicorp/hcl-lang/schema"
	tfschema "github.com/opentofu/opentofu-schema/schema"
	"github.com/zclconf/go-cty/cty"
	ctyjson "github.com/zclconf/go-cty/cty/json"
)

type object = map[string]any

func main() {
	if len(os.Args) != 2 {
		fmt.Fprintln(os.Stderr, "usage: ekn-tofuschema <tofu-version>")
		os.Exit(2)
	}
	if err := run(os.Args[1]); err != nil {
		fmt.Fprintf(os.Stderr, "ekn-tofuschema: %v\n", err)
		os.Exit(1)
	}
}

func run(raw string) error {
	v, err := version.NewVersion(raw)
	if err != nil {
		return err
	}
	core, err := tfschema.CoreModuleSchemaForVersion(v)
	if err != nil {
		return err
	}
	out, err := body(core)
	if err != nil {
		return err
	}
	encoder := json.NewEncoder(os.Stdout)
	encoder.SetEscapeHTML(false)
	return encoder.Encode(out)
}

func body(b *schema.BodySchema) (object, error) {
	out := object{}
	if b == nil {
		return out, nil
	}
	attributes := object{}
	for name, a := range b.Attributes {
		converted, err := attribute(a)
		if err != nil {
			return nil, fmt.Errorf("%s: %w", name, err)
		}
		attributes[name] = converted
	}
	out["attributes"] = attributes
	blocks := object{}
	for name, bl := range b.Blocks {
		converted, err := block(bl)
		if err != nil {
			return nil, fmt.Errorf("%s: %w", name, err)
		}
		blocks[name] = converted
	}
	out["blocks"] = blocks
	if b.AnyAttribute != nil {
		converted, err := attribute(b.AnyAttribute)
		if err != nil {
			return nil, fmt.Errorf("any attribute: %w", err)
		}
		out["any_attribute"] = converted
	}
	if e := b.Extensions; e != nil {
		out["count"] = e.Count
		out["for_each"] = e.ForEach
		out["dynamic_blocks"] = e.DynamicBlocks
	}
	return out, nil
}

var nesting = map[schema.BlockType]string{
	schema.BlockTypeNil:    "",
	schema.BlockTypeObject: "single",
	schema.BlockTypeList:   "list",
	schema.BlockTypeSet:    "set",
	schema.BlockTypeMap:    "map",
}

func block(b *schema.BlockSchema) (object, error) {
	labels := make([]string, len(b.Labels))
	for i, label := range b.Labels {
		labels[i] = label.Name
	}
	mode, ok := nesting[b.Type]
	if !ok {
		return nil, fmt.Errorf("block type %v", b.Type)
	}
	converted, err := body(b.Body)
	if err != nil {
		return nil, err
	}
	out := object{
		"labels":    labels,
		"nesting":   mode,
		"min_items": b.MinItems,
		"max_items": b.MaxItems,
		"body":      converted,
	}
	// The bodies chosen by the first label, such as a backend's by its type.
	// Keys on attributes (a module's source) need the module's metadata,
	// which a core schema does not have.
	dependent := object{}
	for key, depBody := range b.DependentBody {
		// Not schema.DependencyKeys: its attribute values hold cty values,
		// which encoding/json cannot decode back.
		var keys struct {
			Labels     []schema.LabelDependent `json:"labels"`
			Attributes []json.RawMessage       `json:"attrs"`
		}
		if err := json.Unmarshal([]byte(key), &keys); err != nil {
			return nil, err
		}
		if len(keys.Attributes) > 0 || len(keys.Labels) != 1 || keys.Labels[0].Index != 0 {
			continue
		}
		convertedDep, err := body(depBody)
		if err != nil {
			return nil, fmt.Errorf("%s: %w", keys.Labels[0].Value, err)
		}
		dependent[keys.Labels[0].Value] = convertedDep
	}
	if len(dependent) > 0 {
		out["dependent"] = dependent
	}
	return out, nil
}

func attribute(a *schema.AttributeSchema) (object, error) {
	c, err := constraint(a.Constraint)
	if err != nil {
		return nil, err
	}
	return object{
		"required":   a.IsRequired,
		"optional":   a.IsOptional,
		"computed":   a.IsComputed,
		"constraint": c,
	}, nil
}

func ctyType(t cty.Type) (json.RawMessage, error) {
	if t == cty.NilType {
		t = cty.DynamicPseudoType
	}
	return ctyjson.MarshalType(t)
}

func constraints(cs []schema.Constraint) ([]any, error) {
	out := make([]any, len(cs))
	for i, c := range cs {
		converted, err := constraint(c)
		if err != nil {
			return nil, err
		}
		out[i] = converted
	}
	return out, nil
}

func constraint(c schema.Constraint) (object, error) {
	switch c := c.(type) {
	case nil:
		return object{"type": "dynamic"}, nil
	case schema.AnyExpression:
		t, err := ctyType(c.OfType)
		return object{"type": t}, err
	case schema.LiteralType:
		t, err := ctyType(c.Type)
		return object{"type": t}, err
	case schema.LiteralValue:
		t, err := ctyType(c.Value.Type())
		return object{"type": t}, err
	case schema.List:
		elem, err := constraint(c.Elem)
		return object{"list": elem, "min_items": c.MinItems, "max_items": c.MaxItems}, err
	case schema.Set:
		elem, err := constraint(c.Elem)
		return object{"set": elem, "min_items": c.MinItems, "max_items": c.MaxItems}, err
	case schema.Map:
		elem, err := constraint(c.Elem)
		return object{"map": elem}, err
	case schema.Object:
		attributes := object{}
		for name, a := range c.Attributes {
			converted, err := attribute(a)
			if err != nil {
				return nil, fmt.Errorf("%s: %w", name, err)
			}
			attributes[name] = converted
		}
		return object{"object": attributes}, nil
	case schema.Tuple:
		elems, err := constraints(c.Elems)
		return object{"tuple": elems}, err
	case schema.OneOf:
		options, err := constraints(c)
		return object{"one_of": options}, err
	case schema.Keyword:
		return object{"keyword": c.Keyword}, nil
	case schema.Reference:
		return object{"reference": true}, nil
	case schema.TypeDeclaration:
		return object{"type_declaration": true}, nil
	}
	return nil, fmt.Errorf("constraint kind %T", c)
}
