# Publication review

The record of what was done to this tree before it was offered for
publication, for the owner to read first. It names no account, instance,
container, person or log; the private details behind each finding were
reported to the owner separately. Delete or keep this file at publication
as you prefer; nothing else refers to it except the README's file table.

## Status in one line

The **tracked tree** is free of deployment identity and secrets. The
**reachable history is not**: its first commit carries an operator handle
and a deployment region that a later commit replaced with generic values.
The owner has decided (section *Decisions taken*) to publish a **fresh
snapshot of the tracked tree without that history**, under the working name
**agentic-on-spot**, publicly, once the final files have been reviewed. The
history stays in the private repository. Nothing has been published,
pushed or exported yet: the export is prepared after the owner's review.

## What this package is, and is not

- Documentation, lessons and validation status for the existing Spot
  handoff notifier, plus small example and ignore-rule fixes. No new
  behaviour, no infrastructure beyond what was already here.
- Prepared offline: no remote was created, nothing was pushed, published
  or deployed, and no network, AWS, Docker or model call was made.
- The repository has no remote. Publication, if any, is a separate step the
  owner takes after reading this.

## Files changed by the architecture guide

Added or changed after the review below, for the owner's request for a
concept-first guide and an agent-readable skill. Still documentation only:
no Lambda code, host script, Terraform module or CloudFormation template
line changed, and no Auto Scaling or storage automation was added.

| File | Change |
| --- | --- |
| `README.md` | Title carries the working public name; a concept-first opening with the three layers and a *pick the components* table that says which piece this repository implements (the notifier) and which are the reader's; links to the architecture page, the setup guide and the skill; the file table lists the new files; the references list the primary sources behind the new claims. The notifier sections are unchanged |
| `docs/architecture.md` | New: a Mermaid diagram separating infrastructure, persistent state and the notification path, the *who does what* table, the interruption in order, and the topology options for the control plane |
| `docs/setup.md` | New: the assembly guide (inventory, decision points, control plane versus workspace containers, Auto Scaling group and launch template, single-writer data volume and Docker data root, SSM and tags and instance role, boot order, the notifier, after an interruption), each section with its check, generic sketches only, primary AWS and Coder documentation cited next to each claim |
| `skills/agentic-on-spot/SKILL.md` | New: the agent entrypoint, name and description in YAML frontmatter, inventory-first workflow, the user's choices, prepare before deploy, deploy only what is authorized, check each step; relative links into the documents above. Not installed anywhere; it lives in the tree |
| `LICENSE` | New: the MIT license text, copyright "agentic-on-spot contributors" (see *Decisions taken*) |
| `tests/test_docs.py` | New: every relative Markdown link resolves, the README links the three new documents, the architecture page carries one Mermaid block, and the skill's frontmatter has a hyphen-case name matching its directory and a plain description |
| `docs/publication-review.md` | This section, *Decisions taken*, the checks table and the checklist |

## Files changed by the first review

| File | Change |
| --- | --- |
| `README.md` | New *Validation status* and *Lessons* sections; the two stale validation bullets under *Limits and non-goals* replaced by the resume/non-goals statement and a pointer; tags and state prerequisites say why they matter; stage 3 uses a git-ignored `my-event.json` and notes that `out.json` carries your identifiers; the repository's test command no longer named with an internal tooling term; the file table lists this document |
| `cloudformation/README.md` | *Status* reflects the owner-reported deployment without reproducing it; step 3 explains the escaped-in-file versus raw-in-console `Targets` spelling and the ignore rule; step 5 says what `CodeSha256` must equal; *Updating* says when the change set executes and that a host-script change is a code change; the closing section says what was not run from here |
| `examples/basic/variables.tf` | The example root module's `targets` type gains `detached = optional(bool, false)`, which the module already had: without it the example's `detached = true` for Hermes did not reach the module, so the example did not deploy the detached launch it documents |
| `examples/basic/render-env.example.json` | The yardr message now matches the tfvars and CloudFormation examples word for word (it was a shorter variant) |
| `scripts/check.sh` | Label of the unit-test step: "the repository's test command" |
| `.gitignore` | `my-*.json` and `out.json`: a real parameter file, an edited event and the output of `aws lambda invoke`; the tracked `*.example.*` files are unaffected |
| `docs/publication-review.md` | This file |

No Lambda code, host script, Terraform module or CloudFormation template
line was changed.

## Audit of the tracked tree and the reachable history

Method: `git grep` over the tracked tree and `git log --all -p` over every
reachable commit on every local branch, with the matched text masked where
it could be private. Patterns: 12-digit numbers (account ids), EC2 instance
ids, UUID-shaped ids (EventBridge event ids, session ids), `coder-*`
container names other than the example ones, AWS access key ids, private
key blocks, secret-like words, email addresses, IPv4 addresses, `s3://`
URIs, ARNs carrying an account, home directories other than the container's
`/home/coder`, internal task-tracker ids, the owner's handles, SSM command
or request ids, AWS hostnames, and common API token shapes. Commit author
and committer identities, commit messages, the stash and dangling objects
were checked as well.

Findings:

- **Tracked tree: clean.** Every 12-digit number, instance id, event id and
  container name is an example or test fixture value (`123456789012`,
  `i-0123456789abcdef0`, `coder-example-*`, and the like). No key, token,
  email, bucket, hostname, log excerpt or tracker id. The only IP address is
  the EC2 metadata endpoint.
- **History: two lines in the first commit** (a docstring example in
  `lambda/spot_handoff/config.py` and a fixture in `tests/support.py`) used
  the operator's own actor handle as the `--actor` example value, and the
  sample event and fixtures named the deployment's region. A later commit
  replaced them with `example-operator` and a generic region. The old lines
  remain in reachable history on `main` and on every branch forked from it.
  No account id, instance id, event id, container name, bucket, key, log or
  tracker id was found anywhere in history.
- **Authorship:** every commit carries one placeholder tooling identity
  with a localhost address, not a personal name or address. Most commit
  messages end with a `Co-Authored-By` trailer naming the AI coding tool
  and its vendor's no-reply address: a disclosure, not a private identity,
  and part of the history decision below.
- **Local, untracked and ignored, in the private checkout only:** a real
  CloudFormation parameter file (excluded through `.git/info/exclude`
  before this review and through `.gitignore` after it; its content never
  entered the git object database, checked by blob hash), built Lambda
  zips, and a Terraform build zip whose file name carries the deployment's
  chosen stack name (inside an ignored directory). None of these is in any
  commit. None was read, moved or deleted.

Consequence: publishing the history as it is would publish the operator
handle and the region. Nothing was rewritten or deleted here, on purpose;
the owner's decision (below) is to publish a snapshot without it.

The audit was repeated on the files the architecture guide added or
changed, with the same patterns: no account id, instance id, event id,
container name other than the `coder-example-*` and generic
`coder-<owner>-<workspace>` forms, bucket, key, hostname, address, handle or
log excerpt. The sketches in `docs/setup.md` carry `<placeholders>`; the
only region named in the new text is the generic one the fixtures already
use. The test fixtures (`123456789012`, `i-0123456789abcdef0`,
`coder-example-*`, `example-operator`) were left as they are.

## Checks run, and skipped

Run in the review worktree, offline, on the edited tree:

| Check | Result |
| --- | --- |
| `python3 -m unittest discover -s tests -v` (the repository's test command) | pass; the run prints the count |
| `scripts/check.sh` with Terraform pointed at a missing binary, so no provider download is attempted | unit tests, `bash -n` of both scripts, `cfn_check.py` on the example parameters and the reproducible packager: PASS |
| `python3 scripts/cfn_check.py --show` on `cloudformation/parameters.example.json` | PASS, two targets, two tags, four IAM statements |
| `cd lambda && python3 -m spot_handoff render --event ../examples/basic/sample-event.json --env ../examples/basic/render-env.example.json --region us-east-2 --account 123456789012 --shell` | renders; the yardr message in the render env now matches the other examples |
| `git check-ignore` on `my-parameters.json`, `my-event.json`, `out.json`, and `git ls-files -i -c` for tracked files the new rules would hide | the three are ignored; no tracked file is |
| The sensitive-pattern audit above | as reported |

Run for the architecture guide, offline, on the edited tree:

| Check | Result |
| --- | --- |
| `python3 -m unittest discover -s tests -v` | pass, 110 ok, 0 failed, on the tree as reviewed |
| `scripts/check.sh` (unit tests, `bash -n`, `cfn_check.py`, the packager; the tool-dependent steps say SKIP) | PASS on what ran |
| The skill-creator validator, `quick_validate.py skills/agentic-on-spot` | run with a minimal stand-in for the `yaml` module, because PyYAML is not installed on the review machine and installing it would need the network: frontmatter keys, name and description rules and the placeholder scan passed. Run it again where PyYAML is installed |
| `tests/test_docs.py` (relative links, README links, Mermaid block, skill frontmatter) | pass |
| Primary documentation behind the new claims | each AWS, Coder and Docker page cited in `docs/setup.md` and `docs/architecture.md` was fetched and read during the writing; the quoted sentences are verbatim from those pages at that time |

Skipped here, because the tool is not installed on the review machine or
would need the network: `shellcheck`, `cfn-lint`, `terraform fmt -check`,
`terraform validate` (needs `terraform init`, which downloads providers),
`aws cloudformation validate-template`, and anything that creates, uploads,
invokes or deploys. cfn-lint 1.57.2 passed on the template as first
committed; a parameter description and one `MinValue` changed in later
commits and cfn-lint has not been rerun on the current template from this
tree, which the README and the CloudFormation page now say. The template
did not change in either review. The Terraform
change in `examples/basic/variables.tf` is a type declaration only; run
`terraform validate` in `examples/basic` where Terraform is available.
The Mermaid diagram was not rendered here (no renderer offline); GitHub
renders `mermaid` fences, and the syntax used (subgraphs, labelled edges,
`classDef`) is the common subset. The operational claims in the setup
guide were checked against the cited primary pages, not against an
account: no Auto Scaling group, volume or host was created or inspected.

## Flaws found, and where they went

- The example root module dropped `detached` (above). Fixed here as an
  example consistency fix; the module itself was correct.
- Nothing else in the Lambda, host script, module or template was found to
  disagree with its documentation during this read.

## Decisions taken

Reported by the owner after the first review, and applied to the tree
where a file was needed. Each still gets the owner's eyes on the final
files before anything is published.

1. **History: a fresh snapshot, no development history.** The publication
   artifact is the tracked tree at the reviewed commit, as one initial
   commit in a new repository. The existing repository, its history, its
   branches and its private, ignored files are kept privately and are not
   rewritten, deleted or published. No remote has been created; the
   coordinator prepares the export after the owner's review of the files.
2. **License: MIT, proposed as the default and selected.** `LICENSE`
   carries the standard MIT text with the copyright line "agentic-on-spot
   contributors": a neutral project attribution, because no personal or
   legal identity was supplied and none was invented. **Flag for the
   owner:** confirm that attribution, or replace it with the name you want
   on the license before publishing; the README's file table and this
   record are the only other places that mention it.
3. **Name and visibility: `agentic-on-spot`, public.** The working public
   name the owner proposed; the README title, the skill name and the
   license line use it. Public visibility after this review and the
   owner's reading of the final files. The local directory and the
   notifier's resource name (`spot-handoff`) are unchanged.
4. **Author identity.** A fresh snapshot carries one commit, authored by
   whoever makes it; the placeholder tooling identity and the
   `Co-Authored-By` trailers of the private history do not travel with it.
5. **Development history is not needed publicly.** Nothing in the public
   tree refers to commit ids, branches or the private tracker.

Still open, optional:

6. **The example agent commands.** The examples carry the real Hermes and
   yardr command shapes with generic container and operator names. They are
   the documented, tested configuration; decide whether a public reader
   should see them as the example or a more neutral `echo` target.
7. **Community files.** No `CONTRIBUTING`, `SECURITY` or code of conduct
   file exists. Optional.
8. **This file.** Keep it as the record, or delete it and the README table
   row at publication.

## Pre-publication checklist

- [x] History decision made: fresh snapshot (carried out at export, after
      the owner's review).
- [x] License chosen and added: MIT, "agentic-on-spot contributors".
- [ ] License attribution confirmed by the owner.
- [x] Name and visibility chosen: `agentic-on-spot`, public.
- [ ] Final files read by the owner, including `docs/setup.md`, whose
      sketches must stay recognisable as sketches.
- [ ] `scripts/check.sh` run where shellcheck, cfn-lint and Terraform are
      installed, with no FAIL.
- [ ] `git status --ignored` in the publishing checkout shows the private
      parameter file and build outputs as ignored, and `git ls-files` shows
      only the files you mean to publish.
- [ ] The README's *Validation status* still describes what has actually
      been run; update it before publishing if the live detached launch or
      a real interruption has been observed since.
