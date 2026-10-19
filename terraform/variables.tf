variable "name" {
  description = "Name for the Lambda function, IAM role, log group and EventBridge rule."
  type        = string
  default     = "spot-handoff"

  validation {
    condition     = can(regex("^[A-Za-z0-9_-]{1,48}$", var.name))
    error_message = "name must be 1-48 characters of [A-Za-z0-9_-]."
  }
}

variable "targets" {
  description = <<-EOT
    Containers to notify on the interrupted host, in parallel. `argv` is the
    exact command run inside the container by `docker exec`; words may contain
    the placeholders {instance_id}, {event_id}, {event_time}, {deadline}.
    `user` is passed to `docker exec -u`; leave it null for the container's
    default user. `detached` (default false) starts the command with
    `docker exec -d`: the host reports the target as "launched" as soon as
    docker has started the process and never waits for it, so
    per_target_timeout bounds the launch only, not the process. Use it for a
    command whose own run outlives the host's window; leave it false where
    the exit status is the evidence. Container names are not verified at plan
    time: the host script reports container-missing when one is not running.
  EOT
  type = list(object({
    name      = string
    container = string
    user      = optional(string)
    detached  = optional(bool, false)
    argv      = list(string)
  }))

  validation {
    condition     = length(var.targets) > 0 && length(distinct([for t in var.targets : t.name])) == length(var.targets)
    error_message = "targets must be non-empty with unique names."
  }
  validation {
    condition     = alltrue([for t in var.targets : can(regex("^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$", t.container)) && length(t.argv) > 0])
    error_message = "every target needs a docker container name and a non-empty argv."
  }
  validation {
    # Same rule as render.py, checked at plan time rather than when the first
    # interruption arrives: every {...} in an argv word must be one of the four
    # placeholders. (render.py also accepts {{ as a literal brace; this check
    # does not, on purpose.)
    condition = alltrue(flatten([for t in var.targets : [for w in t.argv :
      length(regexall("\\{[^{}]*\\}", w)) == length(regexall("\\{(instance_id|event_id|event_time|deadline)\\}", w))
    ]]))
    error_message = "argv words may only contain the placeholders {instance_id}, {event_id}, {event_time} and {deadline}."
  }
}

variable "expected_tags" {
  description = <<-EOT
    Tags every affected instance must carry, exactly, before a command is
    sent. Checked twice: by the Lambda through DescribeInstances (fail closed)
    and by IAM through ssm:resourceTag conditions on ssm:SendCommand.
  EOT
  type        = map(string)

  validation {
    condition     = length(var.expected_tags) > 0
    error_message = "expected_tags must name at least one tag."
  }
}

variable "forbidden_containers" {
  description = "Container names the Lambda refuses to target whatever `targets` says. The Coder control server is `coder` in the Compose stack this was built for."
  type        = list(string)
  default     = ["coder"]
}

variable "state_dir" {
  description = "Directory on the host for per-event dedup state. Must be on the persistent volume (/data) for dedup to survive a redelivery; falls back to a tmpfs dir, with a note in the output, when unusable."
  type        = string
  default     = "/data/spot-handoff"
}

variable "execution_timeout" {
  description = "SSM AWS-RunShellScript executionTimeout in seconds for the whole host script."
  type        = number
  default     = 90
}

variable "submission_timeout" {
  description = "SSM SendCommand TimeoutSeconds: if the agent has not started the command by then it never runs. SSM's minimum is 30."
  type        = number
  default     = 30

  validation {
    condition     = var.submission_timeout >= 30 && var.submission_timeout <= 2592000
    error_message = "submission_timeout must be within SSM's TimeoutSeconds range 30..2592000."
  }
}

variable "per_target_timeout" {
  description = "Seconds each `docker exec` may take before it is killed. Targets run in parallel, so per_target_timeout + inspect_timeout + 10s grace must stay below execution_timeout."
  type        = number
  default     = 60
}

variable "inspect_timeout" {
  description = "Seconds allowed for `docker inspect` before the daemon is declared unavailable."
  type        = number
  default     = 10
}

variable "dry_run" {
  description = "off: real. lambda: validate and log but never call SendCommand. host: SendCommand runs the host script with --dry-run, which checks docker and the containers but execs nothing."
  type        = string
  default     = "off"

  validation {
    condition     = contains(["off", "lambda", "host"], var.dry_run)
    error_message = "dry_run must be off, lambda or host."
  }
}

variable "lambda_runtime" {
  description = "Lambda Python runtime identifier. The code is plain stdlib + boto3 and runs on python3.12 through python3.14; the tests in this repository ran on 3.12."
  type        = string
  default     = "python3.12"

  validation {
    condition     = can(regex("^python3\\.1[2-9]$", var.lambda_runtime))
    error_message = "lambda_runtime must be python3.12 or newer."
  }
}

variable "lambda_architecture" {
  description = "Lambda instruction set architecture."
  type        = string
  default     = "arm64"

  validation {
    condition     = contains(["arm64", "x86_64"], var.lambda_architecture)
    error_message = "lambda_architecture must be arm64 or x86_64."
  }
}

variable "lambda_timeout" {
  description = "Lambda timeout in seconds. The function makes two SDK calls with 3s connect / 10s read timeouts and two attempts each, so 60s is ample."
  type        = number
  default     = 60
}

variable "lambda_memory_size" {
  description = "Lambda memory in MB."
  type        = number
  default     = 128
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for the Lambda log group."
  type        = number
  default     = 30
}

variable "event_bus_name" {
  description = "EventBridge bus that receives the EC2 events. AWS service events arrive on `default`."
  type        = string
  default     = "default"
}

variable "max_event_age_seconds" {
  description = "TTL of a notice, in seconds from the event's own time: how long EventBridge and Lambda keep retrying a failed delivery/invocation, and the age beyond which the Lambda ignores an event (MAX_EVENT_AGE). A notice older than the two-minute window is useless, so this is short on purpose (Lambda's minimum is 60)."
  type        = number
  default     = 180

  validation {
    condition     = var.max_event_age_seconds >= 60 && var.max_event_age_seconds <= 21600
    error_message = "max_event_age_seconds must be 60..21600."
  }
}

variable "max_retry_attempts" {
  description = "Asynchronous invocation retries after a raised error (API failure, timeout). Validation failures and tag mismatches return normally and are never retried."
  type        = number
  default     = 2

  validation {
    condition     = var.max_retry_attempts >= 0 && var.max_retry_attempts <= 2
    error_message = "max_retry_attempts must be 0..2."
  }
}

variable "tags" {
  description = "Tags applied to every resource this module creates."
  type        = map(string)
  default     = {}
}
