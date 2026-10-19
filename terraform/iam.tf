# Least privilege for the Lambda role.
#
# ssm:SendCommand needs two allow statements because the call names two
# resources (the document and the instance) and the tag condition applies to
# the instance only:
#   * the public AWS-RunShellScript document in this region (no account in
#     the ARN for AWS-owned documents);
#   * instances in this account and region that carry every expected tag
#     (ssm:resourceTag/<Key>, see "Restricting Run Command access based on
#     tags" in the Systems Manager user guide).
# ec2:DescribeInstances has no resource-level permissions, so its Resource is
# "*" by necessity; it is read-only and what the Lambda uses to fail closed.

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    sid     = "LambdaAssumeRole"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
    # Confused-deputy guard: only this account's Lambda service may assume it.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = var.name
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = var.tags
}

data "aws_iam_policy_document" "lambda" {
  statement {
    sid       = "WriteOwnLogGroup"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.lambda.arn}:*"]
  }

  statement {
    sid       = "DescribeInstancesForTagCheck"
    actions   = ["ec2:DescribeInstances"]
    resources = ["*"]
  }

  statement {
    sid       = "SendRunShellScriptDocument"
    actions   = ["ssm:SendCommand"]
    resources = ["arn:${local.partition}:ssm:${local.region}::document/AWS-RunShellScript"]
  }

  statement {
    sid       = "SendCommandToTaggedInstances"
    actions   = ["ssm:SendCommand"]
    resources = ["arn:${local.partition}:ec2:${local.region}:${local.account_id}:instance/*"]

    dynamic "condition" {
      for_each = var.expected_tags
      content {
        test     = "StringEquals"
        variable = "ssm:resourceTag/${condition.key}"
        values   = [condition.value]
      }
    }
  }
}

resource "aws_iam_role_policy" "lambda" {
  name   = "${var.name}-least-privilege"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda.json
}
