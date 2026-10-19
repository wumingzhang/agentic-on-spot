# EC2 Spot interruption warning -> EventBridge -> Lambda -> SSM Run Command
# -> docker exec in the configured workspace containers on the affected host.
#
# Region, account and partition come from the provider and the caller, never
# from variables, so the same module deploys anywhere; the Lambda cross-checks
# the event's region/account against its own context at runtime.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
data "aws_partition" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region
  partition  = data.aws_partition.current.partition

  # The Lambda reads TARGETS as JSON; null users and the default detached are
  # dropped so the payload matches what config.py documents.
  targets_json = jsonencode([
    for t in var.targets : merge(
      { name = t.name, container = t.container, argv = t.argv },
      t.user == null ? {} : { user = t.user },
      t.detached ? { detached = true } : {}
    )
  ])

  event_pattern = {
    source        = ["aws.ec2"]
    "detail-type" = ["EC2 Spot Instance Interruption Warning"]
    detail        = { "instance-action" = ["terminate"] }
  }
}

# --- Lambda package ----------------------------------------------------------

data "archive_file" "lambda" {
  type        = "zip"
  source_dir  = "${path.module}/../lambda"
  output_path = "${path.module}/.build/${var.name}.zip"
  excludes    = ["**/__pycache__", "**/__pycache__/**"]
}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.name}"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

resource "aws_lambda_function" "this" {
  function_name    = var.name
  description      = "Notifies workspace containers on a Spot instance that it is about to be interrupted (notification, not checkpointing)."
  role             = aws_iam_role.lambda.arn
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  handler          = "spot_handoff.handler.lambda_handler"
  runtime          = var.lambda_runtime
  architectures    = [var.lambda_architecture]
  timeout          = var.lambda_timeout
  memory_size      = var.lambda_memory_size
  tags             = var.tags

  environment {
    variables = {
      TARGETS              = local.targets_json
      EXPECTED_TAGS        = jsonencode(var.expected_tags)
      FORBIDDEN_CONTAINERS = join(",", var.forbidden_containers)
      STATE_DIR            = var.state_dir
      EXECUTION_TIMEOUT    = tostring(var.execution_timeout)
      SUBMISSION_TIMEOUT   = tostring(var.submission_timeout)
      PER_TARGET_TIMEOUT   = tostring(var.per_target_timeout)
      INSPECT_TIMEOUT      = tostring(var.inspect_timeout)
      DRY_RUN              = var.dry_run
      # Same bound as the EventBridge target and Lambda async retry policies,
      # so a notice older than this is dropped at every layer.
      MAX_EVENT_AGE = tostring(var.max_event_age_seconds)
      # The Lambda also derives the account from its invoked ARN; this makes
      # the expectation explicit in the deployed configuration.
      EXPECTED_ACCOUNT_ID = local.account_id
    }
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.lambda.name
  }

  depends_on = [aws_iam_role_policy.lambda]

  lifecycle {
    # The same budget config.py enforces (per-target + inspect + two 5 s
    # kill-after graces must fit inside executionTimeout), checked at plan
    # time: a Lambda that refuses its own configuration would fail on every
    # event, including the dry-run stages.
    precondition {
      condition     = var.per_target_timeout + var.inspect_timeout + 10 < var.execution_timeout
      error_message = "per_target_timeout + inspect_timeout + 10 must be less than execution_timeout (defaults 60 + 10 + 10 < 90)."
    }
  }
}

# A notice is worthless minutes later: cap how long failed invocations retry.
resource "aws_lambda_function_event_invoke_config" "this" {
  function_name                = aws_lambda_function.this.function_name
  maximum_event_age_in_seconds = var.max_event_age_seconds
  maximum_retry_attempts       = var.max_retry_attempts
}

# --- EventBridge -------------------------------------------------------------

resource "aws_cloudwatch_event_rule" "spot_interruption" {
  name           = var.name
  description    = "EC2 Spot Instance Interruption Warning (terminate) -> ${var.name}"
  event_bus_name = var.event_bus_name
  event_pattern  = jsonencode(local.event_pattern)
  tags           = var.tags
}

resource "aws_cloudwatch_event_target" "lambda" {
  rule           = aws_cloudwatch_event_rule.spot_interruption.name
  event_bus_name = var.event_bus_name
  target_id      = "lambda"
  arn            = aws_lambda_function.this.arn

  retry_policy {
    maximum_event_age_in_seconds = var.max_event_age_seconds
    maximum_retry_attempts       = 3
  }
}

resource "aws_lambda_permission" "eventbridge" {
  statement_id  = "AllowEventBridgeRule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.this.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.spot_interruption.arn
}
