terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 3.51.0"
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.2.1"
    }
  }
  required_version = ">= 0.13"
}
