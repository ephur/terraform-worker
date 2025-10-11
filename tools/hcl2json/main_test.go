package main

import (
    "encoding/json"
    "os"
    "testing"

    convert "github.com/tmccombs/hcl2json/convert"
)

func TestConvertBasicTerraform(t *testing.T) {
    hclData := []byte(`
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 4.0.0"
    }
  }
}
`)
    b, err := convert.Bytes(hclData, "inmem", convert.Options{})
    if err != nil {
        t.Fatalf("convert.Bytes error: %v", err)
    }
    if len(b) == 0 {
        t.Fatalf("expected non-empty JSON output")
    }
}

func TestConvertFixtureVersionsTF(t *testing.T) {
    // Use a repo fixture file to ensure we handle real Terraform syntax
    fp := "../../tests/fixtures/definitions/test_a/versions.tf"
    b, err := os.ReadFile(fp)
    if err != nil {
        t.Fatalf("read fixture: %v", err)
    }
    jb, err := convert.Bytes(b, fp, convert.Options{})
    if err != nil {
        t.Fatalf("convert fixture: %v", err)
    }
    var m map[string]any
    if err := json.Unmarshal(jb, &m); err != nil {
        t.Fatalf("unmarshal json: %v", err)
    }
    if _, ok := m["terraform"]; !ok {
        t.Fatalf("expected 'terraform' key in converted output")
    }
}
