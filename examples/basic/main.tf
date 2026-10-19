# Example deployment. Copy terraform.tfvars.example to terraform.tfvars and
# put your real container names and fleet tags there; terraform.tfvars is
# git-ignored so deployment identifiers stay out of this repository.
#
# The region comes from the provider (AWS_REGION / AWS_DEFAULT_REGION or your
# profile); it must be the region of the Spot instances.

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0.0, < 7.0.0"
    }
  }
}

provider "aws" {}

module "spot_handoff" {
  source = "../../terraform"

  name          = var.name
  targets       = var.targets
  expected_tags = var.expected_tags
  dry_run       = var.dry_run
  tags          = var.tags
}

output "lambda_function_name" {
  value = module.spot_handoff.lambda_function_name
}

output "event_rule_arn" {
  value = module.spot_handoff.event_rule_arn
}

output "log_group_name" {
  value = module.spot_handoff.log_group_name
}

output "lambda_policy_json" {
  description = "Rendered least-privilege policy, for review before apply."
  value       = module.spot_handoff.lambda_policy_json
}
