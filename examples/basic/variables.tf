variable "name" {
  type    = string
  default = "spot-handoff"
}

variable "targets" {
  # Same shape as the module's `targets` (terraform/variables.tf). `detached`
  # has to be declared here as well: an attribute this type does not name
  # never reaches the module, and the example's `detached = true` for Hermes
  # would be lost on the way.
  type = list(object({
    name      = string
    container = string
    user      = optional(string)
    detached  = optional(bool, false)
    argv      = list(string)
  }))
}

variable "expected_tags" {
  type = map(string)
}

variable "dry_run" {
  description = "Start with \"lambda\", then \"host\", then \"off\" (see README, Testing)."
  type        = string
  default     = "lambda"
}

variable "tags" {
  type    = map(string)
  default = {}
}
