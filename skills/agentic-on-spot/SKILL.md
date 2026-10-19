---
name: agentic-on-spot
description: Assemble or audit agent workspaces (Coder, Docker) on an EC2 Spot host with the Spot interruption notifier from this repository. Use when asked to set up, review or extend a Spot-hosted workspace fleet or to deploy the notifier; not for general Coder administration or unrelated AWS work.
---

# agentic-on-spot

The repository implements one thing: a notifier that turns an EC2 Spot
interruption warning into a command inside each workspace container
(`docker exec`), so the agents there can hand their work off. The Coder
control plane, the Auto Scaling group, the data volume and whatever resumes
the work are the user's infrastructure. Do not present any of them as
something this repository deploys.

Read, in this order, and only as far as the task needs:

1. [`README.md`](../../README.md): what the notifier does and does not do,
   its configuration, deployment and the staged rollout.
2. [`docs/architecture.md`](../../docs/architecture.md): the three layers,
   the diagram, who does what, the interruption in order.
3. [`docs/setup.md`](../../docs/setup.md): the assembly guide, with the
   decision points, the configuration interfaces and a check per section.
4. [`cloudformation/README.md`](../../cloudformation/README.md) only for a
   console deployment of the notifier.

## Workflow

**Inventory first.** Before proposing anything, establish what exists:
the Coder deployment and where it runs, the AWS account and region, the
agents and their container names, any Spot host, AMI or data volume and
its Availability Zone, and what already resumes work after a restart
(`docs/setup.md`, section 0). Read it from the user's files and from
read-only commands they let you run; do not guess a value that a command
can print.

**Choose the pieces with the user.** Walk the decision table in
`docs/setup.md` and record each choice. Two are not negotiable because the
hardware is: an EBS volume is bound to one Availability Zone and has one
writer, so a reattached data volume means one AZ, capacity one and
Capacity Rebalancing off; and no lifecycle hook delays a Spot interruption.
Everything else (control plane placement, snapshot versus reattach,
instance types, Terraform versus CloudFormation, which agents are targets)
is the user's call. Where a choice differs from the guide's suggestion, say
why it is fine or what it costs, then follow it.

**Prepare before you deploy.** Produce the concrete inputs and keep them
out of the tree unless they are generic: a filled copy of
`examples/basic/terraform.tfvars.example` as `terraform.tfvars`, or of
`cloudformation/parameters.example.json` as `my-parameters.json` (both are
git-ignored), the launch-template and bootstrap values for the user's own
tooling, and the list of commands you intend to run. Run the offline checks
that need nothing from the account first:

```bash
python3 -m unittest discover -s tests -v
scripts/check.sh
python3 scripts/cfn_check.py --parameters my-parameters.json --region <region> --account <account> --show
cd lambda && python3 -m spot_handoff render --event ../examples/basic/sample-event.json --env <your env file> --region <region> --account <account> --shell
```

**Deploy only what is authorized, where it is authorized.** This skill
grants nothing. Assume no credentials, no permissions and no budget beyond
what the user has given for the task at hand; a `terraform apply`, a stack
creation, an upload, a Lambda invocation or an instance termination is a
separate action each time, taken with the user's go-ahead for that action
and that account. The notifier's rollout is staged on purpose (README,
*Testing*): keep `dry_run` at `lambda` until the user moves it, and never
interrupt a production instance to test.

**Check after each step, with the step's own check.** Every section of
`docs/setup.md` ends with the commands that prove it; the notifier's stages
3 to 5 prove delivery. Report statuses as the README defines them
(`submitted`, `Success`, `delivered`, `launched`, `duplicate`, `failed`),
never collapsed into "it worked". A `launched` detached target is a started
process, not a written note.

## Constraints that are easy to get wrong

- Target containers by their `coder-<owner>-<workspace>` names from
  `docker ps` on the host; never the control server container.
- The data volume is mounted before Docker starts, and Docker's data root
  is on it; a host that boots without it must not start the workspaces.
- Two hosts never attach the same volume; the bootstrap waits for
  `available` and never force-detaches.
- A manual notifier invocation needs a fresh event `id` and a current
  `time` every time (README, *Lessons* 4).
- Account, region, instance, container and operator names stay in the
  git-ignored inputs, not in tracked files.
