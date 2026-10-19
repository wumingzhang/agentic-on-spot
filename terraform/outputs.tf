output "lambda_function_name" {
  description = "Name of the Lambda function; use it with `aws lambda invoke` and `aws logs tail`."
  value       = aws_lambda_function.this.function_name
}

output "lambda_function_arn" {
  value = aws_lambda_function.this.arn
}

output "lambda_role_arn" {
  value = aws_iam_role.lambda.arn
}

output "event_rule_arn" {
  description = "ARN of the EventBridge rule; the Lambda invoke permission is scoped to it."
  value       = aws_cloudwatch_event_rule.spot_interruption.arn
}

output "log_group_name" {
  value = aws_cloudwatch_log_group.lambda.name
}

output "lambda_policy_json" {
  description = "The rendered least-privilege policy, for review."
  value       = data.aws_iam_policy_document.lambda.json
}
