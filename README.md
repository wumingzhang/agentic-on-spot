# agentic-on-spot: agent workspaces on EC2 Spot, with a Spot interruption notifier

Agent workspaces (Coder workspaces running as Docker containers) on an EC2
Spot host are cheap and get reclaimed with about two minutes' warning. The
arrangement that makes that survivable has three layers: your
infrastructure (a Coder control plane, wherever it runs, and a Spot host
that an Auto Scaling group replaces), your persistent state (one durable
data volume mounted before Docker starts, so the workspaces, the agents'
records and the handoff notes outlive the instance), and the notification
path that tells the agents the host is about to go. **This repository
implements the notification path only.** The other two layers are
described so you can build them to your needs; nothing here deploys them.

| Piece | Who supplies it | Where to read |
| --- | --- | --- |
| Spot interruption notifier: EventBridge rule, Lambda, SSM Run Command, host script, `docker exec` per target | **this repository** (Terraform module or CloudFormation template, your choice) | this page, then `cloudformation/README.md` for the console path |
| Coder control plane | you; on the Spot host, on a separate instance, or managed elsewhere | [`docs/setup.md`](docs/setup.md), section 1 |
| Spot host: Auto Scaling group, launch template, image, bootstrap, tags, instance role | you | `docs/setup.md`, sections 2, 4 and 5 |
| Durable storage: EBS data volume (one Availability Zone, one writer), Docker data root on it, snapshots | you | `docs/setup.md`, section 3 |
| The agents' own persistence and whatever resumes their work after a restart | you, or the agents' existing schedulers and runners | `docs/setup.md`, section 7 |

The diagram and the division of responsibilities are in
[`docs/architecture.md`](docs/architecture.md). An agent asked to assemble
or audit such a setup starts from the skill in
[`skills/agentic-on-spot/SKILL.md`](skills/agentic-on-spot/SKILL.md),
which follows the same documents in the same order. Everything below this
line is the notifier itself.

## The notifier

When AWS warns that an EC2 Spot instance will be terminated, tell the agent
workspaces running in Docker containers on that instance, so they can put
their work into handoff mode before the host disappears.

```text
EC2 Spot Instance Interruption Warning (instance-action = terminate)
  -> EventBridge rule (default bus, same region)
  -> Lambda (Python): validate event, DescribeInstances tag check, SendCommand
  -> SSM Run Command, AWS-RunShellScript, InstanceIds = [the affected instance]
  -> SSM Agent on the host runs the embedded host script as root
  -> in parallel, one per configured target:
       docker exec -d -u coder <hermes-container> /home/coder/.local/bin/hermes chat --oneshot --run-budget 45 -q '<recovery-note prompt, one argument>'   (detached: reported "launched")
       docker exec -u coder    <yardr-container>  /home/coder/.local/bin/yardr --home /home/coder/.yardr --actor <user> mail yardmaster '<notice, deadline, handoff request>'   (synchronous: reported "delivered")
```

> **This is a notification, not a synchronous checkpoint.**
> A Spot notice gives about two minutes, on a best-effort basis. EventBridge,
> Lambda, SSM delivery, `docker exec` and the agents' own reaction all eat
> into it. A `SendCommand` that is accepted proves only that SSM took the
> request; the host script's exit status proves only that the commands inside
> the containers returned. Nothing here waits for agents to finish saving
> state, and nothing can extend the deadline. Work that must survive an
> interruption has to be persisted continuously by the agents themselves,
> on storage that outlives the instance.

## What is in the repository

| Path | Purpose |
| --- | --- |
| `lambda/spot_handoff/handler.py` | Lambda entry point: validation, tag check, `SendCommand`, structured logs |
| `lambda/spot_handoff/validation.py` | Strict event, instance id, tag and argv rules |
| `lambda/spot_handoff/config.py` | Configuration from environment variables (targets, tags, timeouts, dry-run) |
| `lambda/spot_handoff/render.py` | Builds the `AWS-RunShellScript` command lines; every word validated and `shlex.quote`d |
| `lambda/spot_handoff/spot-handoff-notify.sh` | Host script, shipped inside the SSM command: parallel bounded `docker exec`, per-event dedup under `/data` |
| `lambda/spot_handoff/__main__.py` | `python3 -m spot_handoff render`: preview the exact SSM request without AWS |
| `terraform/` | Module: EventBridge rule and target, Lambda zip and function, least-privilege IAM, log group |
| `examples/basic/` | Example root module, `terraform.tfvars.example`, sample event, render env |
| `cloudformation/` | The same deployment as a CloudFormation template for the AWS console, with its own README and example parameters |
| `scripts/package-lambda.py` | Builds the reproducible Lambda zip the CloudFormation path uploads to S3 |
| `scripts/cfn_check.py` | Offline check of the template against a parameter file: the Lambda's own validation plus IAM/EventBridge parity with Terraform |
| `tests/` | Stdlib `unittest` suite, including the host script under a fake `docker` and the template evaluated for several parameter sets |
| `scripts/check.sh` | Runs everything local: unit tests, `bash -n`, shellcheck, `cfn_check.py`, the packager, cfn-lint, `terraform fmt`/`validate` (skips what is not installed) |
| `docs/architecture.md` | The three layers (your infrastructure, your persistent state, this notifier), as a Mermaid diagram with who does what and the interruption in order |
| `docs/setup.md` | Assembly guide for the pieces around the notifier: Coder control plane placement, Auto Scaling group and launch template, the single-writer data volume and Docker data root, SSM and tags, boot order; decision points and a check per section. Describes, does not deploy |
| `skills/agentic-on-spot/SKILL.md` | Agent entrypoint: inventory first, choose the pieces, prepare inputs, deploy only what is authorized, check each step; links the documents above |
| `docs/publication-review.md` | Pre-publication record: what was audited and checked before this tree was offered for publication, the owner's decisions and what remains to confirm (the owner's record; removable once published) |
| `LICENSE` | MIT, attributed to the project's contributors |

Nothing deployment-specific lives here: container names, fleet tags, account
and region are variables or derived at apply time. The files that carry
yours are git-ignored: `terraform.tfvars`, a `my-*.json` parameter or event
file, and the `out.json` an invocation writes. Keep anything else
deployment-specific outside the repository.

## How it behaves

**Event filtering.** The EventBridge pattern matches only
`source = aws.ec2`, `detail-type = EC2 Spot Instance Interruption Warning`,
`detail.instance-action = terminate`. The Lambda re-checks all three and
additionally requires: a well-formed `detail.instance-id`, an `id`, a `time`,
and `region` and `account` equal to the Lambda's own (region from the
runtime, account from the invoked function ARN or `EXPECTED_ACCOUNT_ID`). When
`resources` is present it must name the same instance. Anything else is
logged as `event ignored` and returns without retry.

**Event TTL.** A notice is only useful inside its two-minute window, so the
same bound (`max_event_age_seconds`, default 180 s) is applied three times:
EventBridge stops retrying delivery to the Lambda after it, Lambda stops
retrying a failed asynchronous invocation after it, and the Lambda itself
ignores an event whose `time` is older than it (logged as `stale event`).
An event timestamped more than 300 s in the future is ignored as well
(`MAX_FUTURE_SKEW`). Setting `MAX_EVENT_AGE=0` disables the Lambda-side
check. The sample event in `examples/basic/sample-event.json` therefore has
to carry a fresh `time` (and a new `id`, for the host dedup) when you invoke
the function by hand.

**Fleet check, fail closed.** `DescribeInstances` must return exactly the
requested instance, carrying every configured expected tag with the exact
value. Missing instance, missing tag, different value, or any API error means
no command is sent. API errors raise so the asynchronous invocation is
retried within the event-age bound; validation failures return and are not.

**Command.** `SendCommand` with `DocumentName = AWS-RunShellScript`,
`InstanceIds = [instance]`, `TimeoutSeconds = 30` (SSM's minimum: if the
agent has not picked the command up by then, it never runs) and
`executionTimeout = 90` for the host script. The `commands` parameter is the
host script itself inside a quoted heredoc, followed by one invocation line.
No event-provided text is ever interpreted by a shell: the instance id, event
id and timestamps pass validation first and are then `shlex.quote`d; the host
script re-validates them and never uses `eval`. Nothing from the event can
choose a container or a command. The rendered request is about 12 KB, below
the 64 KB SSM document limit.

**On the host.** The script runs every target at once in its own subshell,
so one hung container cannot starve the other. Per target: take a
`flock` on `<state_dir>/events/<event-id>/<target>.lock`; if a `.done` marker
exists, report `duplicate`; otherwise check `docker` is on PATH, `docker
inspect` (bounded by `--inspect-timeout`, default 10 s) says the container is
running, then run `timeout --kill-after=5 <per-target-timeout> docker exec
[-u user] <container> <argv...>` (default 60 s). A zero exit writes the
`.done` marker atomically; a non-zero exit, a timeout, a missing container or
an unreachable daemon leaves no marker, so a redelivery of the same event
retries exactly the targets that have not succeeded. A concurrent invocation
for the same event sees the lock held and reports `in-progress` without
duplicating. Worst case per invocation is about 80 s, inside the 90 s
`executionTimeout`.

**Detached targets.** A target with `detached = true` is started with
`docker exec -d` instead: docker returns as soon as the process is running in
the container, the `.done` marker is written then (it reads `launched`, not
`delivered`), and the target is reported as `launched`. The per-target
timeout still bounds that launch (a wedged daemon is reported as a failed
launch, with no marker, so a redelivery launches again), but **nothing bounds
the detached process**: the host script does not wait for it, does not watch
it, does not kill it, and cannot tell whether it finished or what it did. A
redelivery of the same event reports `duplicate` and starts no second
process. `launched` is deliberately weaker than `delivered`: it says a
process was started, never that a recovery note or anything else was
written. Use it only for a command whose own run outlives the host's window
and which is harmless to leave running. There is no background collector,
watchdog or resume mechanism behind it, by design.

**Output.** One JSON line per target
(`status` is `delivered`, `launched`, `duplicate`, `in-progress`, `dry-run`
or `failed`, with `rc`, `elapsed_s` and a bounded `detail`) plus a `summary`
line that counts `delivered` and `launched` separately. Exit status 0 when no
target failed, 1 when any failed (SSM shows the invocation as `Failed`), 2 on
bad arguments. The last 4 KB of each container's output is kept in
`<state_dir>/events/<event-id>/<target>.log`; for a detached target that is
docker's own output of the launch (a start error), never the process's.

**Duplicates and at-least-once.** EventBridge and Lambda deliver at least
once; the host dedups per EventBridge event id, durably when `/data` is
there. Two *different* events for the same instance (unusual) notify twice
on purpose. The yardr mail has no idempotency key and the Hermes oneshot
rewrites one fixed note file, so a duplicate notification is possible in
the rare cases the state is lost (host state dir unusable, crash between
`docker exec` returning and the marker being written). Both commands were
chosen to be harmless when repeated.

**Dedup state.** `STATE_DIR` defaults to `/data/spot-handoff` because `/data`
is the host's persistent volume in the setup this was written for (Docker's
data root and the Compose stack live there). The script creates the
directory. Event directories older than `--retention-days` (14) are pruned
on each run. If the directory cannot be created or written, the script
falls back to `$TMPDIR/spot-handoff-state`, still delivers, and the summary
line carries `note: ... dedup not durable`. No DynamoDB or other AWS state.

## Prerequisites

On every Spot instance that should be notified:

- **SSM**: SSM Agent installed and running; the instance profile allows
  `AmazonSSMManagedInstanceCore` or equivalent; outbound HTTPS to the `ssm`,
  `ssmmessages` and `ec2messages` endpoints of the region (internet, NAT or
  VPC endpoints). Check with
  `aws ssm describe-instance-information --filters Key=InstanceIds,Values=<i-...>`:
  `PingStatus` must be `Online`.
- **Tags**: every key/value in `expected_tags`, on each instance itself,
  propagated from the Auto Scaling group or launch template so replacements
  carry them too. Both the Lambda and IAM require them: an instance without
  them is never notified, and the only trace is a `rejected` line in the
  Lambda log, so check one fresh replacement after a launch template change.
- **Host tools**: `bash`, `docker`, `flock`, `timeout`, `mktemp`, `find`
  (util-linux and coreutils; present on Amazon Linux 2023). The SSM agent
  runs commands as root, which is what `docker exec` needs.
- **State**: `/data` mounted and writable before the containers start
  (whatever starts the containers on the host should refuse to start without
  it), with a few MB free for `/data/spot-handoff`. The dedup markers live
  there, so a redelivery after a reboot is still recognised.
- **Containers**: the workspace containers exist under the configured names
  and are running; `/home/coder/.local/bin/hermes` exists in the Hermes
  container, user `coder` can run it, and `/home/coder/.hermes/handoffs/` is
  writable by that user (the recovery note is written there);
  `/home/coder/.local/bin/yardr`
  exists in the yardr container and user `coder` can run it with
  `--home /home/coder/.yardr`. Container names follow
  Coder's docker template convention `coder-<owner>-<workspace>`; confirm
  with `docker ps --format '{{.Names}}'` on the host. **Never** target the
  Coder control server container `coder`; the Lambda refuses it by default
  (`forbidden_containers`).

For deploying, either Terraform >= 1.5 with provider access to the registry
and AWS credentials, or the CloudFormation console plus an S3 bucket of your
own in the target region for the Lambda zip; both need rights to create IAM
roles/policies, Lambda functions, EventBridge rules and log groups there. No
credentials are stored in or read by this repository.

## Configuration

Set in `terraform.tfvars` (see `examples/basic/terraform.tfvars.example`) or
as CloudFormation parameters with the same meaning and defaults
(`cloudformation/README.md` maps the names; the one deliberate difference is
that the template's `DryRun` defaults to `lambda`):

| Variable | Meaning |
| --- | --- |
| `targets` | List of `{name, container, user (optional), detached (optional, default false), argv}`; `argv` words may use `{instance_id}`, `{event_id}`, `{event_time}`, `{deadline}` (any other `{...}` is rejected at plan time); `detached` must be a boolean when present, anything else (`"true"`, `1`, `null`) is rejected |
| `expected_tags` | All must match on the instance, checked by the Lambda and by IAM |
| `forbidden_containers` | Default `["coder"]`; refused whatever `targets` says |
| `state_dir` | Default `/data/spot-handoff` |
| `execution_timeout`, `submission_timeout`, `per_target_timeout`, `inspect_timeout` | 90 / 30 / 60 / 10 seconds; `terraform plan` refuses a combination that cannot fit (per-target + inspect + 10 s grace must stay below `execution_timeout`), and the Lambda checks the same rule. For a detached target `per_target_timeout` bounds the `docker exec -d` launch only, never the lifetime of the process it started |
| `dry_run` | `off`, `lambda`, or `host` (below) |
| `lambda_runtime`, `lambda_architecture`, `lambda_timeout`, `lambda_memory_size` | `python3.12`, `arm64`, 60 s, 128 MB |
| `log_retention_days`, `max_event_age_seconds`, `max_retry_attempts` | 30 days, 180 s (EventBridge retry age, Lambda retry age and Lambda-side TTL), 2 |

The Hermes target runs as the container user `coder` (the host script
passes `-u coder`, the short form of `docker exec --user coder`), detached
(`docker exec -d`), and its argv is passed verbatim, the binary by its
absolute path inside the container:

```text
/home/coder/.local/bin/hermes chat --oneshot --run-budget 45 -q '<prompt>'
```

with this prompt, as **one** argument:

> AWS Spot interruption: this host is about to be terminated. Create a recovery note in /home/coder/.hermes/handoffs/spot-latest.md. Inspect persisted recent sessions and task records. Record: - active or incomplete task IDs and artifact paths; - last verified progress; - next actions; - uncertain external side effects that must be checked before retrying. Do not start implementation, deploy, send messages, or change task status. Do not claim other running agents have checkpointed unless verified. If time is short, save a partial note immediately.

The owner supplied this command after consulting Hermes (an owner-confirmed
syntax). The prompt was supplied as several lines; an argv word cannot carry
a newline (validation rejects control characters, see `validation.py`), so
the lines are joined with single spaces into one single-line argument with
the same words and bullets, which is the form every example in this
repository uses. Do not relax the validation to carry the line breaks.

**Why Hermes is detached.** The owner's live test of the synchronous form
showed the oneshot writing its recovery note (about 7.8 KB) and yet being
killed by the host's 60 s `per_target_timeout` (`timeout` exit 124), so the
run was reported as `failed` although the note existed, and a redelivery
would have started the oneshot again. With `detached = true` the host
starts the oneshot with `docker exec -d`, writes the dedup marker as soon as
docker has started the process, and reports `launched`. `--run-budget 45`
is then Hermes's own bound on its run; the host neither waits for it nor
ends it. What Hermes does with the prompt is Hermes's: it reads the
persisted session and task records it has and writes a best-effort note. A
`launched` status proves only that the process was started, not that a note
was written, let alone that every live session had saved its state; a
synchronous `delivered` proved that the command returned zero, which is
more, and that is why the two are reported and counted apart. Nothing in
this repository collects the outcome of a detached process or terminates
it; on a real interruption the host disappears about two minutes later, and
in a rehearsal on a host that stays up the process simply runs to its own
end. The yardr target stays synchronous: `yardr mail` returns quickly and
its exit status is the evidence that the mail was accepted. It runs as the
container user `coder` and mails the yardmaster a message built only from
validated event fields (it must stay under yardr's 4 KB mail limit; the
example is about 250 bytes).

The `Targets` value this gives, as one line of JSON (the CloudFormation
parameter; Terraform's `targets` has the same shape in HCL, see
`examples/basic/terraform.tfvars.example`), with the example container names
and operator to be replaced by yours:

```json
[{"name":"hermes","container":"coder-example-hermes","user":"coder","detached":true,"argv":["/home/coder/.local/bin/hermes","chat","--oneshot","--run-budget","45","-q","AWS Spot interruption: this host is about to be terminated. Create a recovery note in /home/coder/.hermes/handoffs/spot-latest.md. Inspect persisted recent sessions and task records. Record: - active or incomplete task IDs and artifact paths; - last verified progress; - next actions; - uncertain external side effects that must be checked before retrying. Do not start implementation, deploy, send messages, or change task status. Do not claim other running agents have checkpointed unless verified. If time is short, save a partial note immediately."]},{"name":"yardr","container":"coder-example-yardr","user":"coder","argv":["/home/coder/.local/bin/yardr","--home","/home/coder/.yardr","--actor","example-operator","mail","yardmaster","Spot interruption warning: instance {instance_id} will be terminated at about {deadline} (notice {event_time}, event {event_id}). Put all tasks into handoff mode now: commit work, note state on the bead, and stop starting new work."]}]
```

Switching a deployment to this configuration changes the host script
(shipped inside the Lambda zip) as well as the targets, so the code has to
be redeployed, not only the environment: with Terraform, `terraform apply`
picks the new `source_code_hash` up on its own; with CloudFormation, build
a new zip (`python3 scripts/package-lambda.py`), upload it under a **new**
S3 key or note the new object version, and update the stack with the new
`LambdaCodeS3Key`/`LambdaCodeS3ObjectVersion` **and** the new `Targets` in
one change set (`cloudformation/README.md`, *Updating*). A function running
the previous zip would reject `--detached-target` with a usage error (exit
2) on every event, which the host dry-run stage shows before anything goes
live.

## Deployment

Two paths deploy the same thing. Pick one per account; do not run both for
the same `name` in one region, they would collide on the function and rule
names. Either way deploy in the region of the instances: EC2 emits the
warning there, the rule listens on that region's default bus, and the Lambda
rejects events from any other region or account.

**Terraform** (the reference; validates targets, tags and the timeout budget
at plan time and zips the code itself):

```bash
cd examples/basic
cp terraform.tfvars.example terraform.tfvars     # edit: real container names, real tags
export AWS_REGION=<region of the Spot instances>  # or a profile that sets it
terraform init
terraform plan -out plan.tfplan
terraform apply plan.tfplan
```

Rendered IAM policy for review: `terraform output -raw lambda_policy_json`.

**CloudFormation from the AWS console** (no Terraform on the machine; the
Lambda zip is built locally and uploaded to a bucket you own):

```bash
python3 scripts/package-lambda.py                                  # build/spot-handoff-lambda.zip + SHA-256
cp cloudformation/parameters.example.json my-parameters.json       # edit; my-*.json is git-ignored
python3 scripts/cfn_check.py --parameters my-parameters.json --show  # Lambda's own validation + IAM parity, prints the policies
```

then upload the zip to your bucket and the template
`cloudformation/spot-handoff.json` through *Create stack* with the
parameters from your file and the `CAPABILITY_IAM` acknowledgement.
`cloudformation/README.md` has the exact steps, the change-set review
workflow, what CloudFormation cannot check that Terraform does, and what was
and was not validated. The template's `DryRun` defaults to `lambda`.

## Testing

Staged, from no side effects to live. Each stage can stay in place as long
as you like.

**1. Local, no AWS.**

```bash
python3 -m unittest discover -s tests -v          # the repository's test command
scripts/check.sh                                   # tests + bash -n + shellcheck + cfn_check + cfn-lint + terraform fmt/validate, skipping missing tools
python3 scripts/cfn_check.py --parameters <your parameters> --show   # CloudFormation path: what the stack would deploy
cd lambda && python3 -m spot_handoff render --event ../examples/basic/sample-event.json \
    --env ../examples/basic/render-env.example.json --region us-east-2 --account 123456789012 --shell
```

The last command prints the exact shell text SSM would run. With
`"DRY_RUN": "host"` in the env file the invocation line carries `--dry-run`,
so the text can be pasted into a root shell on a host to check docker and
the containers without executing anything inside them.

**2. Manual host run (non-production host, as root).** Run the host script
directly with `--dry-run` first:

```bash
bash spot-handoff-notify.sh --event-id manual-$(date +%s) --instance-id "$(curl -s -H "X-aws-ec2-metadata-token: $(curl -s -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')" http://169.254.169.254/latest/meta-data/instance-id)" \
  --dry-run --detached-target hermes coder-example-hermes coder 7 /home/coder/.local/bin/hermes chat --oneshot --run-budget 45 -q 'AWS Spot interruption: this host is about to be terminated. Create a recovery note in /home/coder/.hermes/handoffs/spot-latest.md. Inspect persisted recent sessions and task records. Record: - active or incomplete task IDs and artifact paths; - last verified progress; - next actions; - uncertain external side effects that must be checked before retrying. Do not start implementation, deploy, send messages, or change task status. Do not claim other running agents have checkpointed unless verified. If time is short, save a partial note immediately.'
```

The dry run prints `would launch detached (not awaited): docker exec -d -u
coder ...` for a detached target and starts nothing. Then without
`--dry-run` with a harmless argv (for example `--target h coder-example-hermes coder 3 echo hello world`),
then with the real argv when you are ready to see the agents react; a
detached target comes back as `launched` at once, and what the oneshot then
did is read from the container (`/home/coder/.hermes/handoffs/spot-latest.md`),
not from this script.

**3. `dry_run = "lambda"` (the example's and the template's default).** Deploy, then invoke with
a copy of the sample event (`my-event.json`, git-ignored) edited to your
account, region, a real instance id, a fresh `id` and a `time` within the
last three minutes (older events are ignored as stale, see Event TTL). Every
later invocation needs a new `id` and a current `time` again: the same `id`
is a `duplicate` to the host from stage 5 on, and a `time` from the last
attempt is soon stale.

```bash
cp examples/basic/sample-event.json my-event.json                 # edit: account, region, instance, id, time
aws lambda invoke --function-name spot-handoff --cli-binary-format raw-in-base64-out \
  --payload file://my-event.json out.json && cat out.json
aws logs tail /aws/lambda/spot-handoff --since 15m
```

Expect `"status": "dry-run"` with the rendered request, or `"ignored"` /
`"rejected"` with a reason. Nothing is sent to SSM. `out.json` carries
your identifiers and the rendered request; it is git-ignored too.

**4. `dry_run = "host"`.** Apply (CloudFormation: *Update stack*, change the `DryRun` parameter), invoke again. Now `SendCommand` is issued
and the host script runs with `--dry-run`: it checks `docker` and each
container and prints the commands it would run. Inspect:

```bash
aws ssm list-command-invocations --command-id <CommandId from out.json or the logs> --details
aws ssm get-command-invocation --command-id <CommandId> --instance-id <i-...>
```

`Status` should be `Success`, `StandardOutputContent` the JSON lines with
`"status":"dry-run"` (a detached target's `detail` starts with `would launch
detached`). A `Failed` invocation with `container-missing` or
`docker-unavailable` names what to fix on the host.

**5. `dry_run = "off"`.** Apply. Invoke once more with a fresh event `id`
(the host dedups per id) on a non-production instance and watch the agents.
Real Spot interruptions now trigger it. Do not interrupt a production
instance on purpose; if you want an end-to-end rehearsal, do it in a
disposable environment (AWS Fault Injection Service has the action
`aws:ec2:send-spot-instance-interruptions` for exactly this).

Negative cases to try at stage 3 or 4: `instance-action = stop` (ignored),
a malformed instance id (ignored), an instance without the tags (rejected),
a stopped container (host reports `container-not-running`), the same event
twice (second run reports `duplicate`), an instance id that no longer
exists (SendCommand fails with `InvalidInstanceId`, logged, retried within
the age bound).

## Operations

**Correlating one interruption.** Lambda log lines are JSON with
`event_id`, `instance_id`, `request_id` and, once accepted, `command_id`:

```bash
aws logs filter-log-events --log-group-name /aws/lambda/spot-handoff --filter-pattern '{ $.instance_id = "i-0123456789abcdef0" }'
aws ssm list-command-invocations --command-id <command_id> --details
```

On the host (if it still exists) `cat /data/spot-handoff/events/<event_id>/*.result`
shows each target's outcome and `*.log` the container output.

**What the statuses mean.** Lambda `submitted` = SSM accepted the request.
Invocation `Success` = host script exit 0 = every target `delivered`,
`launched` or `duplicate`. `delivered` = the synchronous command returned
zero. `launched` = `docker exec -d` started the detached process; its
completion is not observed, so this is not a delivered recovery note, only a
started attempt at one. Invocation `Failed` = at least one target failed;
read the target lines. `DeliveryTimedOut` = the agent did not pick the
command up within 30 s (host already gone, agent offline).
`InvalidInstanceId` from `SendCommand` = instance not managed, agent down,
or already shutting down.

**Disarming without destroying.** Set `dry_run = "lambda"` and apply (or
update the stack's `DryRun` parameter): the rule and function stay, nothing
reaches SSM. Or disable the rule: `aws events disable-rule --name spot-handoff`.

**Rollback.** `terraform destroy` in the example directory, or deleting the
CloudFormation stack, removes the rule, target, permission, function,
event-invoke config, role, policy and log group (log retention is enforced
by the group; destroying deletes it). No host-side artefacts need removal
beyond `rm -rf /data/spot-handoff` if you want the dedup state gone.

**Changing targets or tags.** Edit `terraform.tfvars` and apply, or update
the stack's parameters. Targets are Lambda environment variables; the host
script is inside the Lambda zip and is redeployed whenever it changes
(Terraform: `source_code_hash`; CloudFormation: a new S3 key or object
version, since an object overwritten in place is not noticed).

## IAM

The role has one inline policy with four statements
(`terraform/iam.tf`):

- `logs:CreateLogStream`, `logs:PutLogEvents` on the module's own log group
  only. The group is created by Terraform with explicit retention, so
  `CreateLogGroup` is not granted.
- `ec2:DescribeInstances` on `*`: the API has no resource-level
  permissions, so the wildcard is unavoidable; it is read-only and is what
  lets the function fail closed.
- `ssm:SendCommand` on the public `AWS-RunShellScript` document of the
  region (AWS-owned documents have no account in their ARN). No other
  document can be run.
- `ssm:SendCommand` on `instance/*` of this account and region, with a
  `StringEquals ssm:resourceTag/<Key>` condition for every expected tag
  (the mechanism described under "Restricting Run Command access based on
  tags" in the Systems Manager user guide). A command to an untagged
  instance is denied by IAM even if the Lambda were tricked.

The Lambda invoke permission is scoped to the rule's ARN; the role trust
policy is scoped to the Lambda service of this account. No Auto Scaling,
launch template or instance-profile permissions are touched by this module.
The CloudFormation template grants the same four statements, split over two
inline policies because the tag-conditioned one has to be rendered as a
string; `scripts/cfn_check.py` asserts that split still equals this list.

## Limits and non-goals

- Notification only. No synchronous checkpointing, no confirmation that
  agents saved anything, no way to delay termination.
- A detached target is a started process and nothing more: no completion
  collector, no watchdog, no automatic resume, and no bound on its lifetime.
  The timeouts here bound the submission of commands, not the processes a
  detached launch leaves behind; do not expect a synthetic test to end them.
- At-least-once with best-effort dedup; a duplicate notification is
  possible when the host state is unavailable.
- Only `instance-action = terminate` is handled. `stop` and `hibernate`
  are ignored on purpose.
- One instance per event, by design; no fleet-wide broadcast and no Auto
  Scaling group changes.
- The optional on-instance metadata watcher (`/latest/meta-data/spot/instance-action`
  polled locally, which would remove EventBridge/Lambda/SSM latency from
  the path) is not part of this repository.
- The Hermes command is owner-confirmed syntax (supplied after consulting
  Hermes) and the actual container names of a deployment are configuration.
  The note Hermes writes comes from the records it has persisted: neither a
  delivered notice nor a launched oneshot is proof that every running
  session checkpointed.
- Resuming the work afterwards is the job of whatever already runs the
  agents: their own persisted sessions and task records, the schedulers and
  runners that pick work up again, and the fleet's replacement instances.
  This repository prepares the notification and the handoff note, nothing
  more: no checkpoint guarantee, no Auto Scaling or storage orchestration,
  no back-online logic.
- What has and has not been exercised is under *Validation status* below.
  Nothing here claims production end-to-end behaviour or Spot reliability.
- Run Command keeps only a bounded prefix of a command's output unless S3 or
  CloudWatch output is configured (not done here); the script keeps its own
  output to a few short JSON lines, and the per-target container logs stay
  on the host.

## Validation status

Candid, as of the commit this file is in. "Owner-reported" means the owner
ran it in their own account and reported the outcome; nothing in this
repository reproduces it, and no log, identifier or account detail from it
is kept here.

**Run from this repository, offline.** The unit suite (`python3 -m unittest
discover -s tests`), `scripts/cfn_check.py` against the example parameters,
the reproducible packager, `bash -n` of the host script and, where
installed, shellcheck, cfn-lint and `terraform fmt`/`validate`. cfn-lint
1.57.2 reported no findings on the template as first committed; a parameter
description and one `MinValue` have changed since, and cfn-lint has not been
rerun on the current template from this tree. The host script runs in the tests under a fake `docker`,
including the detached launch, dedup, timeouts and the failure modes. No
test here talks to AWS, Docker or an agent.

**Owner-reported, one real deployment.**

- The deployment was taken through the staged rollout. The Lambda, invoked
  by hand with a fresh event at the live stage, had its `SendCommand`
  accepted, and the SSM invocation ran the host script on a tagged instance.
- The synchronous yardr target was delivered: `yardr mail` returned 0.
- The Hermes oneshot, while it still ran synchronously, was killed once at
  the 60 s per-target bound (`timeout` exit 124) although it had written
  its note, and in a later manual invocation returned 0 after 57 s. A fresh
  recovery note file was observed afterwards; its content was not
  independently reviewed for accuracy.
- After the switch to `detached = true`, the host dry-run stage passed with
  the new code (`would launch detached` for Hermes, nothing started), and
  the owner reports the update complete.

**Not verified, by anyone.**

- A detached Hermes launch through the live path (`dry_run = "off"`, the
  command delivered by SSM, `docker exec -d` on the host). The owner chose to
  wait for real Spot activity rather than trigger a synthetic interruption,
  so this has not been seen.
- An end-to-end run triggered by a real EventBridge Spot interruption
  warning, including the host disappearing about two minutes later.
- Anything about how often a notice arrives in time, is delivered in time,
  or leads to a useful note. No claim is made about production reliability.

**Timing data that does not transfer.** Two independent, concise Hermes
oneshot runs with a short prompt took 13.8 s and 11.7 s. That is not the
broad session-and-task discovery the real prompt asks for, and it says
nothing about whether the real oneshot fits a synchronous bound; the 57 s
and the exit 124 above do.

## Lessons

Generalisable, from building and deploying this.

1. **Report what you know, in layers.** *Submitted* (SSM accepted the
   request), *Success* (the host script exited 0), *delivered* (a
   synchronous command returned 0) and *launched* (a detached process was
   started) are four different claims. Collapsing them hides the failure
   that matters: a note that was never written.
2. **Do not size a synchronous timeout from a different workload.** A short
   rehearsal prompt ran in twelve to fourteen seconds; the real prompt ran 57 s
   once and past 60 s another time. Where the duration belongs to a tool
   you do not control, let the tool bound itself (`--run-budget`) and
   detach, rather than tuning the outer bound until it happens to pass.
3. **A detached launch controls nothing after the launch.** The per-target
   timeout bounds `docker exec -d`, not the process it started. There is no
   completion collector, no watchdog and no automatic termination: in a
   rehearsal on a host that stays up, a detached test process runs to its
   own end, and you stop it yourself inside the container. Detach only a
   command that is harmless to leave running and harmless to repeat.
4. **A replayed event is not a fresh event.** The Lambda ignores a notice
   older than the TTL (180 s by default) and the host dedups per event id
   under a persistent state directory. A manual invocation therefore needs
   a new `id` and a current `time` every time, or the redelivery you meant
   to test shows up as `duplicate` or `stale event` instead of a delivery.
5. **CloudFormation redeploys code only on a new key or object version.**
   Overwriting the zip in place changes nothing; a new `LambdaCodeS3Key` or
   `LambdaCodeS3ObjectVersion` does. Review the change set (a *Modify* of
   the function, not a *Replace*), execute it, then compare the deployed
   `CodeSha256` with the hash the packager printed.
6. **The console takes raw JSON; the parameters file takes it escaped.** In
   a parameters file the `Targets` value is a JSON string, so every inner
   quote is backslash-escaped; in the console's parameter field you paste
   the one-line JSON as it is. The escaped form pasted into the console
   deploys a Lambda that refuses its configuration on every event
   (`configuration invalid` in its log), which the `lambda` dry-run stage
   shows before anything reaches SSM. `scripts/cfn_check.py` validates the
   file's spelling, not what was typed into the console.
7. **Tags are a per-instance contract.** The Lambda and IAM both require
   every expected tag on the instance itself. A replacement launched
   without them is never notified and leaves only a `rejected` log line:
   propagate the tags from the Auto Scaling group or launch template and
   check one fresh replacement.
8. **Keep deployment identity out of the tree from the first commit.**
   Account, region, instance, container and operator names belong in
   git-ignored inputs; examples carry generic values and a test keeps them
   generic. Scrubbing the tree later still leaves the history to deal with.

## Versions and references

- Terraform `>= 1.5.0` (checked with 1.16.6); `hashicorp/aws >= 6.0, < 7.0`
  (6.69.1 at the time of writing; the module uses `data.aws_region.current.region`,
  the v6 attribute); `hashicorp/archive >= 2.4, < 3.0` (2.8.1).
- Lambda runtime `python3.12` by default (deprecation Oct 2028); `python3.13`
  and `python3.14` are also supported runtimes and the code needs nothing
  newer than 3.12.
- Spot interruption notice event shape and two-minute best-effort warning:
  <https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/spot-instance-termination-notices.html>
- `SendCommand` parameters and errors (`TimeoutSeconds` 30..2592000,
  `Comment` <= 100, `InvalidInstanceId`):
  <https://docs.aws.amazon.com/systems-manager/latest/APIReference/API_SendCommand.html>
- Tag-restricted Run Command IAM (`ssm:resourceTag/<Key>`):
  <https://docs.aws.amazon.com/systems-manager/latest/userguide/run-command-setting-up.html>
- Lambda runtimes: <https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtimes.html>
- Auto Scaling lifecycle hooks cannot hold a Spot interruption; Capacity
  Rebalancing overlaps the old and the new host on purpose:
  <https://docs.aws.amazon.com/autoscaling/ec2/userguide/lifecycle-hooks.html>,
  <https://docs.aws.amazon.com/autoscaling/ec2/userguide/ec2-auto-scaling-capacity-rebalancing.html>
- EBS volumes attach within one Availability Zone, to one instance unless
  Multi-Attach (`io1`/`io2`, cluster file system required):
  <https://docs.aws.amazon.com/ebs/latest/userguide/ebs-attaching-volume.html>,
  <https://docs.aws.amazon.com/ebs/latest/userguide/ebs-volumes-multi.html>
- The other primary sources behind `docs/setup.md` are listed at its end.
