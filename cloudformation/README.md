# Deploying from the AWS console with CloudFormation

`spot-handoff.json` is the console-deployable twin of the `terraform/`
module: the same Lambda code, the same environment, the same IAM statements,
the same EventBridge rule and TTL bounds, resource for resource. Read the
top-level README first for what the system does and does not do; this page
only covers getting it into an account through the CloudFormation console.

> **Status.** Checked offline from this repository: the unit tests
> (`tests/test_cloudformation.py`), `scripts/cfn_check.py` and cfn-lint
> 1.57.2. The owner reports one real deployment taken through the staged
> rollout, with a manually invoked notice delivered through SSM (top-level
> README, *Validation status*); a live detached launch and a real Spot
> interruption have not been observed through either path. Nothing from
> that deployment (account, bucket, names, logs) is in this repository, and
> the staged rollout below is how you validate your own.

## Where to review the implementation

| What | Where |
| --- | --- |
| Behaviour and limits | `README.md` (How it behaves, Limits and non-goals) |
| Lambda: validation, tag check, SendCommand | `lambda/spot_handoff/handler.py`, `validation.py`, `config.py`, `render.py` |
| Host script that runs inside the SSM command | `lambda/spot_handoff/spot-handoff-notify.sh` |
| IAM, as Terraform renders it | `terraform/iam.tf`; the template's `LambdaRole` resource mirrors it |
| IAM, as the template renders it for your parameters | `python3 scripts/cfn_check.py --parameters <your file> --show` |
| Exact SSM request for an event, no AWS needed | `cd lambda && python3 -m spot_handoff render ...` (README, Testing) |
| Tests | `tests/`: the stdlib `unittest` suite (the host script under a fake `docker`, the template under `cfn_check.py`) |

## What CloudFormation cannot do that Terraform does

- **Loop over the tag map.** The template has four tag slots
  (`ExpectedTag1Key/Value` .. `ExpectedTag4Key/Value`); slot 1 is required,
  the others optional. Both the Lambda's `EXPECTED_TAGS` and the IAM
  `ssm:resourceTag/<Key>` conditions are built from the same slots, so they
  cannot disagree. More than four tags: extend the slots or use Terraform.
- **Check the timeout budget or the Targets JSON at deploy time.**
  CloudFormation Rules have no arithmetic and cannot parse a string. The
  Lambda refuses an unfit configuration on every event (`configuration
  invalid` in its log), so run `scripts/cfn_check.py` on your parameters
  before you deploy: it applies exactly the Lambda's checks.
- **Zip the code.** Lambda needs the code from an S3 bucket in the stack's
  region (the inline `ZipFile` form takes one file of at most 4 KB; the
  package is seven files). You build the zip locally and upload it to a
  bucket you own; the template points at it.

## Step by step

### 0. Prerequisites

- On the Spot instances: everything under *Prerequisites* in the top-level
  README (SSM Agent online, the expected tags, docker/bash/flock/timeout,
  `/data` mounted, the containers running under the configured names).
- An S3 bucket **in the region of the Spot instances**, which is the region
  the stack must be created in. Versioning on the bucket is recommended so
  `LambdaCodeS3ObjectVersion` can pin the exact reviewed artifact.
- Console permissions to create a stack with IAM resources: CloudFormation,
  Lambda, EventBridge, CloudWatch Logs and `iam:CreateRole`/`PutRolePolicy`.
  The template creates one role with two inline policies and lets
  CloudFormation name it, so the stack needs the `CAPABILITY_IAM`
  acknowledgement only, not `CAPABILITY_NAMED_IAM`.
- Locally: Python 3.12+, this repository checked out at the reviewed commit.

### 1. Build the Lambda artifact

```bash
python3 -m unittest discover -s tests          # the whole suite, no AWS
python3 scripts/package-lambda.py              # -> build/spot-handoff-lambda.zip
```

The packager prints the zip's size, file count, SHA-256 and the base64
`CodeSha256`. The zip is reproducible: the same tree gives the same bytes, so
the reviewer and the owner can build independently and compare hashes, and
after deployment `aws lambda get-function-configuration --function-name
spot-handoff --query CodeSha256` must print the base64 value.

### 2. Upload the artifact to your bucket

Console: S3 > your bucket > Upload > `build/spot-handoff-lambda.zip`, under a
key of your choice, for example `spot-handoff/spot-handoff-lambda.zip`. Or:

```bash
aws s3 cp build/spot-handoff-lambda.zip s3://<your-bucket>/spot-handoff/spot-handoff-lambda.zip
aws s3api head-object --bucket <your-bucket> --key spot-handoff/spot-handoff-lambda.zip --query VersionId
```

Note the version id if the bucket is versioned. CloudFormation fetches the
object when the stack is created or when `LambdaCodeS3Key` or
`LambdaCodeS3ObjectVersion` changes; overwriting the object under the same
key does **not** redeploy the function (see *Updating*).

### 3. Write down and check your parameters

Copy `parameters.example.json`, replace the example values with yours
(bucket, key, container names, tags), keep `DryRun` at `lambda`, then:

```bash
python3 scripts/cfn_check.py --parameters my-parameters.json --region <region> --account <account-id> --show
```

`--show` prints the two IAM policy documents and the Lambda environment
exactly as they will be deployed, for review. `PASS` means: every required
parameter present and within its constraints, the template's Rules hold, the
Lambda's own `load_config` accepts the environment (targets, forbidden
containers, placeholders, timeout budget), the rendered SSM command fits the
document limit, and the IAM/EventBridge structure matches Terraform. Keep
`my-parameters.json` private: it carries your bucket, container names and
tags. The repository's `.gitignore` covers `my-*.json`, so a copy under that
name inside the checkout is not committed; a copy under another name is
yours to keep out.

The console asks for each parameter by name; the file is your record of what
you typed and the input to the checker. Mind the two spellings of `Targets`:
in the file it is a JSON string inside JSON, so every inner quote is
escaped (`[{\"name\":\"hermes\", ...`); in the console's parameter field
you paste the raw one-line JSON (`[{"name":"hermes", ...`), no backslashes.
The escaped form pasted into the console deploys a Lambda that refuses its
configuration on every event (`configuration invalid` in the log), which
stage 3 of the rollout shows before anything reaches SSM.

### 4. Create the stack

CloudFormation console, in the Spot instances' region:

1. **Stacks > Create stack > With new resources (standard).**
2. **Prepare template**: choose an existing template. **Template source**:
   upload a template file, `cloudformation/spot-handoff.json`. (The console
   copies it to an S3 bucket it manages; that is normal.)
3. **Specify stack details**: stack name (for example `spot-handoff`), then
   the parameters, grouped as in the checker. Required:
   `LambdaCodeS3Bucket`, `Targets`, `ExpectedTag1Key`, `ExpectedTag1Value`.
   Leave `DryRun` at `lambda`.
4. **Configure stack options**: stack tags propagate to every resource the
   stack creates (Terraform's `tags` variable). Leave rollback on failure
   enabled. Under *Capabilities*, acknowledge that CloudFormation might
   create IAM resources (`CAPABILITY_IAM`).
5. **Review**: the page shows the template, parameters and options. Submit.

Creation takes about a minute. On the stack's *Events* tab a failure names
the resource and reason (a wrong bucket or key, or a bucket in another
region, fails `LambdaFunction` with the S3 error in the status reason) and
the stack rolls back, leaving nothing behind. *Outputs* shows the function name, its
ARN, the role ARN, the rule ARN and the log group.

To review before anything is created, create a change set for a new stack
instead. Console versions differ in whether the wizard's review page offers
*Create change set* next to *Submit*; the CLI always can. The stack then
sits in `REVIEW_IN_PROGRESS` with no resources until you execute the change
set from its *Change sets* tab or the CLI:

```bash
aws cloudformation create-change-set --stack-name spot-handoff --change-set-name initial \
  --change-set-type CREATE --template-body file://cloudformation/spot-handoff.json \
  --parameters file://my-parameters.json --capabilities CAPABILITY_IAM
aws cloudformation describe-change-set --stack-name spot-handoff --change-set-name initial
aws cloudformation execute-change-set --stack-name spot-handoff --change-set-name initial
```

`aws cloudformation validate-template --template-body
file://cloudformation/spot-handoff.json` checks syntax against the live
service without creating anything; it was not run here (no AWS access).

### 5. Review what was deployed

```bash
aws iam list-role-policies --role-name <LambdaRoleArn's last segment>
aws iam get-role-policy --role-name <role> --policy-name spot-handoff-least-privilege
aws iam get-role-policy --role-name <role> --policy-name spot-handoff-tagged-instances
aws lambda get-function-configuration --function-name spot-handoff   # Environment, CodeSha256, Timeout
aws events describe-rule --name spot-handoff                         # EventPattern: terminate only
aws lambda get-function-event-invoke-config --function-name spot-handoff
aws lambda get-policy --function-name spot-handoff                   # invoke permission scoped to the rule ARN
```

The two policies together are the four statements of `terraform/iam.tf`;
the `tagged-instances` one carries a `StringEquals` on
`ssm:resourceTag/<Key>` for every tag slot you filled. `CodeSha256` must
equal the base64 value `scripts/package-lambda.py` printed for the zip you
uploaded; a different value means the function runs another object (an
older key, or the object as it was before an in-place overwrite).

### 6. Staged rollout, as in the top-level README

Stage 3 of *Testing* in the top-level README applies as is: with `DryRun =
lambda` invoke the function with a copy of the sample event (`my-event.json`,
git-ignored) edited to your account, region, a real instance id, a fresh
`id` and a fresh `time`, and expect
`"status": "dry-run"` plus the rendered request; nothing reaches SSM. Then
stages 4 and 5, changing the `DryRun` parameter through **Update stack**:

1. Stack > **Update** > *Use existing template* > change `DryRun` to `host`
   (stage 4) and later `off` (stage 5) > acknowledge capabilities.
2. The review page shows the **change set preview**: the only change is
   `LambdaFunction`, *Modify*, no replacement. Submit.

Updating a Lambda environment variable takes seconds and loses no state; the
host dedup markers live on the instances, not in the stack.

## Updating

- **New code** (a new reviewed commit): rebuild the zip, upload it under a
  **new key** or note the **new object version**, then Update stack with the
  new `LambdaCodeS3Key`/`LambdaCodeS3ObjectVersion`. The change set shows
  `LambdaFunction` *Modify*; the console executes it when you submit, the
  CLI on `execute-change-set`. Compare `CodeSha256` afterwards. A change to
  the host script is a code change too (it ships inside the zip), so a new
  target shape and the code that understands it go in one change set
  (top-level README, *Configuration*).
- **New targets or tags**: Update stack with the new parameter values;
  run `scripts/cfn_check.py` on them first. Changing a tag slot modifies
  both the Lambda environment and the role's inline policy in one change
  set.
- **New template version**: Update stack > *Replace existing template* >
  upload the new file; review the change set preview before submitting. A
  change that *Replaces* `LambdaFunction` or `SpotInterruptionRule` means a
  short gap in coverage; a Modify does not.
- **Disarm without deleting**: set `DryRun` to `lambda`, or
  `aws events disable-rule --name spot-handoff`.
- **Rollback**: delete the stack. It removes the rule, target, permission,
  function, event-invoke config, role, policies and the log group (and its
  logs). The zip stays in your bucket; host state stays in
  `/data/spot-handoff` on the instances.

## Parameters

| Parameter | Default | Terraform equivalent |
| --- | --- | --- |
| `Name` | `spot-handoff` | `name` (function, rule and log group; the role is named by CloudFormation) |
| `LambdaCodeS3Bucket`, `LambdaCodeS3Key`, `LambdaCodeS3ObjectVersion` | required / `spot-handoff-lambda.zip` / empty | `archive_file` + `filename` |
| `Targets` | required | `targets`, as one line of JSON; a target's optional `"detached":true` is the Terraform `detached = true` |
| `ExpectedTag1Key`/`Value` .. `ExpectedTag4Key`/`Value` | slot 1 required, 2..4 empty | `expected_tags` |
| `ForbiddenContainers` | `coder` | `forbidden_containers`, comma-separated |
| `StateDir` | `/data/spot-handoff` | `state_dir` |
| `DryRun` | **`lambda`** | `dry_run` (the module's default is `off`; the example's is `lambda`) |
| `ExecutionTimeout`, `SubmissionTimeout`, `PerTargetTimeout`, `InspectTimeout` | 90, 30, 60, 10 | same names; budget checked by `cfn_check.py` and the Lambda |
| `LambdaRuntime`, `LambdaArchitecture`, `LambdaTimeout`, `LambdaMemorySize` | `python3.12`, `arm64`, 60, 128 | same |
| `LogRetentionDays`, `MaxEventAgeSeconds`, `MaxRetryAttempts` | 30, 180, 2 | same |
| `EventBusName` | `default` | `event_bus_name` |

Stack tags replace the module's `tags` variable. Region, account and
partition come from the stack (`AWS::Region`, `AWS::AccountId`,
`AWS::Partition`), never from parameters; `EXPECTED_ACCOUNT_ID` is set to
the stack's account, as Terraform sets it to the caller's.

## Checks run on this template, and not run

Run here, offline, on the committed template and `parameters.example.json`:

- `python3 -m unittest discover -s tests` (includes `tests/test_cloudformation.py`);
- `python3 scripts/cfn_check.py` (also via `scripts/check.sh`);
- `cfn-lint cloudformation/spot-handoff.json` with cfn-lint 1.57.2: no findings
  on the template as first committed. Since that run a parameter description
  and one `MinValue` have changed and cfn-lint has not been rerun on the
  current template from this repository; run it before you deploy.
  `scripts/check.sh` runs it when it is on PATH (`pipx install cfn-lint`) and
  says `SKIP` otherwise. cfn-lint checks resource properties against the
  CloudFormation schemas but does not look inside the `tagged-instances`
  policy, which is a string assembled with `Fn::Join`/`Fn::Sub`: a template
  whose rendered policy is not valid JSON still lints clean. `cfn_check.py`
  renders and parses that policy for your parameters, and IAM rejects a
  malformed one when the stack is created (the role fails, the stack rolls
  back).

Not run from this repository: `aws cloudformation validate-template`, any
stack or change set creation, any upload, any Lambda or SSM invocation. The
owner's deployment (top-level README, *Validation status*) is reported, not
reproduced here. Offline checks prove the template is well-formed and means
what the Terraform module means; only a stack in your account proves it
deploys, and only the staged rollout proves delivery.
