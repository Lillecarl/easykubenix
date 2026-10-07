module github.com/lillecarl/ekn-tofuschema

go 1.25

// The versions tofu-ls 0.5.3 builds with, so the core schema is the language
// server's own.
replace github.com/hashicorp/hcl-lang => github.com/opentofu/hcl-lang v0.0.0-20260707084237-1d4085a34474

require (
	github.com/hashicorp/go-version v1.9.0
	github.com/hashicorp/hcl-lang v0.0.0-20260227034452-913389926489
	github.com/opentofu/opentofu-schema v0.4.3
	github.com/zclconf/go-cty v1.19.0
)

require (
	github.com/agext/levenshtein v1.2.1 // indirect
	github.com/apparentlymart/go-textseg/v15 v15.0.0 // indirect
	github.com/apparentlymart/go-textseg/v17 v17.0.1 // indirect
	github.com/hashicorp/errwrap v1.0.0 // indirect
	github.com/hashicorp/go-multierror v1.1.1 // indirect
	github.com/hashicorp/hcl/v2 v2.23.0 // indirect
	github.com/hashicorp/terraform-json v0.27.2 // indirect
	github.com/hashicorp/terraform-svchost v0.1.1 // indirect
	github.com/mitchellh/go-wordwrap v0.0.0-20150314170334-ad45545899c7 // indirect
	github.com/opentofu/registry-address v0.0.0-20230922120653-901b9ae4061a // indirect
	golang.org/x/mod v0.30.0 // indirect
	golang.org/x/net v0.47.0 // indirect
	golang.org/x/sync v0.18.0 // indirect
	golang.org/x/text v0.31.0 // indirect
	golang.org/x/tools v0.39.0 // indirect
)
