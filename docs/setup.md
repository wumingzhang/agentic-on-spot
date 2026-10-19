# Setup guide: assembling agent workspaces on a Spot host

This page explains how the pieces around the notifier fit together and what
you have to decide and supply. It is a guide with configuration interfaces,
decision points and validation steps, not an automation: the only thing this
repository deploys is the notifier (README, *Deployment*). Everything else
here is yours to build, in Terraform, CloudFormation, the console or by
hand, and the examples are generic sketches of the shape, not files to
apply.

Read [`architecture.md`](architecture.md) first for the picture and the
boundary. Each section below ends with how to check that the piece is in
place before moving to the next.

## 0. Inventory first

Before choosing anything, write down what already exists. The skill at
[`skills/agentic-on-spot/SKILL.md`](../skills/agentic-on-spot/SKILL.md)
follows the same order for an agent doing this work.

| Question | Why it matters |
| --- | --- |
| Is there a Coder deployment already, and where does it run? | Decides the topology (section 1) and whether the Docker provider reaches the Spot host |
| Which AWS account and region, and who holds credentials? | The notifier deploys in the instances' region; nothing here assumes credentials or permissions you have not granted |
| Which agents run in workspaces, under which container names, and where do they persist state? | Decides the notifier's targets and what the data volume must hold |
| Is there a Spot host already, or an AMI, or nothing? | Decides whether section 2 is an audit or a build |
| Is there a data volume, and in which Availability Zone? | Fixes the AZ of the Spot host (section 3) |
| What already resumes work after a restart? | The notifier only notifies; resumption is that mechanism's job |

## Decision points

| Decision | Options | Default suggestion | Section |
| --- | --- | --- | --- |
| Where the Coder control plane runs | same Spot host; separate On-Demand instance; managed elsewhere | Keep what you have; if starting from nothing, a separate small On-Demand instance keeps the control plane out of the interruption | 1 |
| How the replacement host gets its state | reattach one persistent data volume (single AZ, live state); new volume from the latest snapshot (any AZ, state as of the snapshot); no shared state (stateless workspaces) | Reattach one volume, single AZ, Capacity Rebalancing off | 3 |
| Capacity Rebalancing | on (replacement launched before the old host is terminated); off | Off with a reattached single-writer volume; on only with the snapshot or stateless pattern | 2 |
| Instance types and AZs in the group | one type, one AZ; several types, one AZ; several AZs | Several types, one AZ, with the reattached volume; several AZs only with the snapshot or stateless pattern | 2 |
| How the notifier is deployed | Terraform; CloudFormation console | Whichever tooling you already use; both deploy the same thing | 6 |
| Which agents are notified, and how | the example Hermes and yardr targets; your own commands | The examples, with your container names | 6 |

## 1. Coder control plane versus workspace containers

Coder has two parts that matter here. `coderd` is the control plane: the
API, the dashboard, the provisioners and the only component that talks to
PostgreSQL. A workspace is "a set of cloud resources", here one Docker
container per workspace, and inside it runs a workspace agent that
"provides a consistent interface for `coderd` and clients"
([Coder architecture](https://coder.com/docs/admin/infrastructure/architecture)).
Workspace agents reach the control plane at its access URL over HTTP or
HTTPS; the control plane never needs an inbound port on the workspace, but
"there must not be a NAT between workspaces and the control plane"
([Coder networking](https://coder.com/docs/admin/networking)).

With the Docker template, the control plane also needs to reach the Docker
daemon that runs the workspace containers. The registry template takes the
daemon address as a variable (`docker_socket`, empty for the local socket)
and names each container `coder-<owner>-<workspace>` with a home volume
`coder-<workspace id>-home` that is kept across restarts
([Docker template on the Coder registry](https://registry.coder.com/templates/coder/docker),
[template from scratch tutorial](https://coder.com/docs/tutorials/template-from-scratch)).
The tutorial's container reaches a control plane on the same Docker host
through `host.docker.internal`; a control plane elsewhere needs its real
access URL instead.

So the interfaces are:

| Interface | Same host | Separate instance or elsewhere |
| --- | --- | --- |
| Docker provider `host` in the template | local socket | `tcp://` with TLS, or `ssh://` to the Spot host; the Spot host must accept it and the address must survive a replacement (an Elastic IP, a private DNS name, or an SSH config updated by your bootstrap) |
| Agent access URL | `host.docker.internal` replacement, as in the tutorial | the control plane's real URL, reachable from the Spot host's VPC |
| Coder database | on the data volume (section 3) or external | wherever the control plane keeps it; the Spot host never touches it |
| What the interruption takes down | control plane and workspaces | workspaces only |

The notifier is indifferent to the choice. It never targets the control
server container (`forbidden_containers`, README *Configuration*), and it
needs only the workspace containers, Docker and the SSM Agent on the host.

**Check.** From where the control plane runs, `docker --host <the same
value> ps --format '{{.Names}}'` lists the workspace containers by their
`coder-<owner>-<workspace>` names. In the Coder dashboard, each workspace's
agent shows as connected after a host restart.

## 2. Workspace host: Auto Scaling group, launch template, image, bootstrap

One Auto Scaling group keeps one Spot host alive. A launch template holds
"the ID of the Amazon Machine Image (AMI), the instance type, a key pair,
security groups, and other parameters used to launch EC2 instances", and
only a launch template, not a launch configuration, can request Spot
capacity with several instance types
([launch templates](https://docs.aws.amazon.com/autoscaling/ec2/userguide/launch-templates.html),
[mixed instances groups](https://docs.aws.amazon.com/autoscaling/ec2/userguide/ec2-auto-scaling-mixed-instances-groups.html)).

What goes where, generically:

| Setting | Where | Value and why |
| --- | --- | --- |
| AMI | launch template | An image with Docker and the SSM Agent; Amazon Linux 2023 ships the agent ([AMIs with SSM Agent preinstalled](https://docs.aws.amazon.com/systems-manager/latest/userguide/ami-preinstalled-agent.html)) and the host tools the script needs (README, *Prerequisites*). Bake Docker in or install it in user data; baking is faster at every replacement |
| Instance profile | launch template | Section 4 |
| Tags | Auto Scaling group with propagate-at-launch, or the launch template's instance tags | Every key in the notifier's `expected_tags`, on the instance itself: "When an Auto Scaling group launches instances, it adds tags to the instances during resource creation" ([tag Auto Scaling groups and instances](https://docs.aws.amazon.com/autoscaling/ec2/userguide/ec2-auto-scaling-tagging.html)). Tags are not propagated to EBS volumes; the data volume is tagged on its own |
| User data | launch template | The bootstrap of section 5. It "run[s] only during the boot cycle when you first launch an instance" and as root ([user data](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/user-data.html)); a replacement is a new instance, so it runs on every replacement |
| Purchase option, instance types | the group's mixed instances policy | Spot, several instance types of similar size, `price-capacity-optimized` allocation; AWS "strongly recommend[s] that you do not use the `lowest-price` allocation strategy" for replacements ([Capacity Rebalancing](https://docs.aws.amazon.com/autoscaling/ec2/userguide/ec2-auto-scaling-capacity-rebalancing.html)) |
| Availability Zones | the group's subnets | **One AZ, the data volume's, with the reattached-volume pattern** (section 3). Several AZs only when the replacement does not need the same volume |
| Capacity | the group | `min 1, max 1, desired 1` with a single reattached volume: a second instance would be a second writer. Larger only for the snapshot or stateless pattern |
| Capacity Rebalancing | the group | **Off with a single reattached volume.** When it is on, "Amazon EC2 Auto Scaling waits until the new instance passes its health check before it terminates the previous instance", so two hosts overlap on purpose, and a bootstrap that attaches the volume would find it busy or, worse, be given it while the old host still writes. On for the snapshot or stateless pattern, where the overlap is the point |
| Lifecycle hooks | the group | Optional, for a graceful stop on *your own* scale-in. They do not extend a Spot interruption: "a lifecycle hook does not prevent an instance from terminating in the event that capacity is no longer available, which can happen at any time with a two-minute interruption notice" ([lifecycle hooks](https://docs.aws.amazon.com/autoscaling/ec2/userguide/lifecycle-hooks.html)) |

A generic sketch of the group, in the CloudFormation dialect because it is
the shortest to read; translate it to your tooling and fill every
placeholder. It is not a file in this repository and nothing here applies
it.

```yaml
# Sketch, not shipped. One Spot host in ONE Availability Zone, no Capacity
# Rebalancing, because section 3 reattaches a single-writer EBS volume.
WorkspaceHostGroup:
  Type: AWS::AutoScaling::AutoScalingGroup
  Properties:
    MinSize: "1"
    MaxSize: "1"
    DesiredCapacity: "1"
    VPCZoneIdentifier: [<subnet in the data volume's AZ>]
    CapacityRebalance: false
    MixedInstancesPolicy:
      InstancesDistribution:
        OnDemandBaseCapacity: 0
        OnDemandPercentageAboveBaseCapacity: 0
        SpotAllocationStrategy: price-capacity-optimized
      LaunchTemplate:
        LaunchTemplateSpecification:
          LaunchTemplateId: <launch template with AMI, instance profile, user data>
          Version: <version>
        Overrides:
          - InstanceType: <type A>
          - InstanceType: <type B of similar size>
    Tags:
      - {Key: Role, Value: example-workspace-host, PropagateAtLaunch: true}
      - {Key: Environment, Value: example, PropagateAtLaunch: true}
```

The tag keys and values are the ones the notifier's examples expect; use
your own and set the same in `expected_tags`.

**Check.** `aws autoscaling describe-auto-scaling-groups` shows one AZ, the
expected capacity, `CapacityRebalance` false (or true, if you chose the
other pattern on purpose); `aws ec2 describe-instances --instance-ids <the
host>` shows every expected tag on the instance; `aws ec2
describe-launch-template-versions` shows the instance profile and user data
you meant.

## 3. Durable storage: the data volume, Docker's data root, snapshots

Everything that must outlive the instance goes on one EBS data volume
mounted at a fixed path (`/data` in this repository's examples) before
Docker starts, and Docker's data root points into it, so that the images,
the containers' writable layers and the named volumes (the Coder home
volumes, which Docker keeps "within a directory on the Docker host" under
its data root, [Docker volumes](https://docs.docker.com/engine/storage/volumes/))
are all on the volume. The notifier's dedup markers default to
`/data/spot-handoff` for the same reason.

Three facts about EBS set the rules here:

- **A volume lives in one Availability Zone.** "You can attach volumes to
  instances that are in the same Availability Zone only"
  ([attach a volume](https://docs.aws.amazon.com/ebs/latest/userguide/ebs-attaching-volume.html)).
  A replacement host in another AZ cannot reattach it. Hence one AZ in
  section 2.
- **One writer.** An ordinary volume attaches to one instance. Multi-Attach
  exists for `io1`/`io2` only, and "standard file systems, such as XFS and
  EXT4, are not designed to be accessed simultaneously by multiple servers"
  ([Multi-Attach](https://docs.aws.amazon.com/ebs/latest/userguide/ebs-volumes-multi.html));
  Docker adds that each daemon needs "a dedicated directory", and sharing
  one "will experience errors that are difficult to troubleshoot"
  ([Docker daemon configuration](https://docs.docker.com/engine/daemon/)).
  So a shared Docker data root between two hosts is never an option, and
  any overlap of two hosts (Capacity Rebalancing, a group with capacity
  above one, a manual replacement started early) must not attach the same
  volume to both.
- **Snapshots cross AZs; live volumes do not.** A snapshot is an incremental
  point-in-time copy, replicated across the region, and "enables you to
  restore volumes in any Availability Zones in that Region"
  ([snapshots](https://docs.aws.amazon.com/ebs/latest/userguide/ebs-snapshots.html)).
  AWS also notes it "does not automatically back up the data stored on your
  EBS volumes".

That gives three patterns:

| Pattern | What the replacement gets | AZ | Overlap allowed | Cost of an interruption |
| --- | --- | --- | --- | --- |
| **Reattach one volume** (suggested) | The live file system, including whatever the agents wrote during the two minutes | The volume's | No: the bootstrap must wait until the volume is `available` and must never force-detach it from a running host | Nothing lost beyond writes in flight |
| **New volume from the latest snapshot** | The state as of the snapshot; a snapshot of a running Docker data root is crash-consistent at best | Any | Yes | Everything since the snapshot, so schedule snapshots to match |
| **Stateless host** | Nothing; the agents keep their state outside the host (a remote repository, an object store, an external database) | Any | Yes | Nothing on the host mattered |

With the first pattern the volume is created once, tagged, `DeleteOnTermination`
is irrelevant because the bootstrap attaches it after launch (volumes attached
after launch are preserved by default, [preserve data](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/preserving-volumes-on-termination.html)),
and the bootstrap attaches it by id or tag. Take snapshots of it anyway, as
the backup; restoring one is the second pattern.

On the host, the volume is mounted by UUID with `nofail`, as the EBS guide
shows, so that a boot without the volume still reaches SSM and can be
diagnosed, and Docker is told where its data root is:

```text
# /etc/fstab, generic; the UUID is the volume's (blkid), the mount point
# is the one the notifier's state_dir and your Compose files use.
UUID=<uuid>  /data  xfs  defaults,nofail  0  2
```

```json
{ "data-root": "/data/docker" }
```

(`/etc/docker/daemon.json`; the default is `/var/lib/docker`,
[Docker daemon configuration](https://docs.docker.com/engine/daemon/).)

**Mount before Docker, always.** If Docker starts before the volume is
mounted it creates a fresh, empty data root on the root disk, the workspace
containers come up without their homes, the agents start from nothing, and
anything they write goes to a disk that dies with the instance. With
systemd, make the Docker unit depend on the mount, for example a drop-in
with `RequiresMountsFor=/data` (and refuse to start the Compose stack if
`/data` is not a mount point, which is what the README's *State*
prerequisite asks for).

**Check.** On the host: `findmnt /data` shows the volume; `docker info
--format '{{.DockerRootDir}}'` prints the path on it; `docker volume ls`
lists the Coder home volumes; after a reboot all three are the same. In the
account: `aws ec2 describe-volumes --volume-ids <id>` shows the AZ of the
group's subnet and one attachment, and `aws ec2 describe-snapshots
--filters Name=volume-id,Values=<id>` shows the backups you expect.

## 4. SSM, tags and the instance role

The notifier reaches the host only through SSM Run Command, and IAM lets it
send a command only to instances carrying the expected tags
(README, *IAM*; [restricting Run Command access based on tags](https://docs.aws.amazon.com/systems-manager/latest/userguide/run-command-setting-up.html#tag-based-access)).
So the host needs, in this order of failure modes you will meet:

1. **An instance profile** whose role allows the SSM Agent to register and
   receive commands (`AmazonSSMManagedInstanceCore` or an equivalent of your
   own), and, with the reattached-volume pattern, `ec2:DescribeVolumes` (which has
   no resource-level permissions) and `ec2:AttachVolume` limited to that
   volume and to instances with the expected tags, so that the bootstrap
   can attach it. No other permission
   is needed for the notifier; the Lambda has its own role.
2. **Network to the SSM endpoints** (`ssm`, `ssmmessages`, `ec2messages`)
   through a NAT, an internet gateway or VPC endpoints
   ([managing EC2 instances with Systems Manager](https://docs.aws.amazon.com/systems-manager/latest/userguide/systems-manager-setting-up-ec2.html)).
3. **The expected tags on the instance itself**, propagated at launch
   (section 2). A replacement without them is never notified and leaves
   only a `rejected` line in the Lambda log (README, *Lessons* 7).
4. **The SSM Agent running** on the AMI you chose.

**Check.** `aws ssm describe-instance-information --filters
Key=InstanceIds,Values=<the host>` shows `PingStatus: Online` for a fresh
replacement, not only for the first host; `aws ec2 describe-instances`
shows every expected tag on it; the notifier's stage 4 dry run
(README, *Testing*) delivers to it.

## 5. Boot order

The sequence a replacement host has to go through, with what breaks when a
step is out of order. Your user data or your image's systemd units
implement it; this repository does not.

| Step | Done by | Must come after | If it does not |
| --- | --- | --- | --- |
| 1. Instance launched with tags, instance profile, in the volume's AZ | Auto Scaling group | | Wrong AZ: the volume cannot attach; no tags: never notified |
| 2. SSM Agent starts | the AMI | 1 | Host unreachable by the notifier and by you |
| 3. Wait until the data volume is `available`, then attach it | bootstrap | 1 | Attaching while the old host still holds it is refused (ordinary volume) or corrupts (Multi-Attach); never force-detach a writer |
| 4. Mount by UUID at the fixed path | bootstrap or fstab | 3 | Docker starts on the root disk with an empty data root |
| 5. Docker daemon starts with `data-root` on the mount | systemd ordering | 4 | Same |
| 6. Workspace containers start (Compose, or Coder starting the workspaces) under their `coder-<owner>-<workspace>` names | your Compose stack or the Coder control plane | 5 | Containers missing: the notifier reports `container-missing` on the next event |
| 7. Workspace agents connect to the control plane | the containers | 6 | Workspaces show as disconnected in Coder |
| 8. Your schedulers or runners read the handoff note and the persisted records and resume | yours | 7 | Work waits until something reads the note; the notifier never does |

A generic user-data sketch of steps 3 to 5, for a host that attaches a tagged
volume itself. Replace every placeholder; it is illustrative, not shipped,
and the waiting loop is the part that enforces the single-writer rule.

```bash
#!/bin/bash
# Sketch, not shipped. Runs as root on first boot (user data).
set -euo pipefail
TOKEN=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
INSTANCE=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id)
VOLUME=<vol-... or a describe-volumes lookup by tag>
UUID=<the file system UUID, the same one /etc/fstab names>

# Single writer: wait for the previous host to release the volume. Never
# force-detach; if this loop times out, stop here and let a person look.
for _ in $(seq 1 60); do
  state=$(aws ec2 describe-volumes --volume-ids "$VOLUME" --query 'Volumes[0].State' --output text)
  [ "$state" = available ] && break
  sleep 5
done
aws ec2 attach-volume --volume-id "$VOLUME" --instance-id "$INSTANCE" --device /dev/sdf
aws ec2 wait volume-in-use --volume-ids "$VOLUME"
# The API reports in-use before the kernel sees the device; wait for it.
for _ in $(seq 1 60); do
  [ -e "/dev/disk/by-uuid/$UUID" ] && break
  sleep 1
done

mkdir -p /data
mount -a                      # /etc/fstab carries the UUID line with nofail
findmnt /data >/dev/null      # refuse to go on without the volume
systemctl start docker        # the unit depends on the mount (RequiresMountsFor=/data)
# then: start the Compose stack, or let the Coder control plane start the workspaces
```

**Check.** Terminate the host on purpose in a disposable environment (or let
the group replace it) and watch the replacement come through every step:
`/var/log/cloud-init-output.log` for the bootstrap, `findmnt /data`, `docker
ps`, the Coder dashboard, and then the notifier's stage 4 dry run against
the new instance id. The AWS Fault Injection Service action
`aws:ec2:send-spot-instance-interruptions` rehearses the real interruption
(README, *Testing*, stage 5).

## 6. The notifier

Only now deploy what this repository ships, with the container names from
section 1, the tags from section 2 and the state directory from section 3:

- Terraform: README, *Deployment*, with `examples/basic/terraform.tfvars.example`.
- CloudFormation from the console: [`../cloudformation/README.md`](../cloudformation/README.md),
  with `cloudformation/parameters.example.json`.

Then the staged rollout in README, *Testing*: local render, manual host run,
`dry_run = lambda`, `dry_run = host`, `dry_run = off`. Each stage is a
validation step for the sections above as much as for the notifier: stage 4
proves SSM, tags, Docker and the container names on a real host at once.

What the targets say is yours. The examples carry the Hermes recovery-note
oneshot (detached, reported `launched`) and the yardr mail (synchronous,
reported `delivered`); the README's *Configuration* explains both and what
each status does and does not prove.

## 7. After an interruption

Nothing in this repository runs after the host script returns. The handoff
note is a file in the Hermes container's home, on the data volume; the
yardr mail is in the yardmaster's inbox, on the data volume. The replacement
host comes up through section 5, the containers start, and whatever already
schedules and runs the agents' work reads the note and the persisted
records and continues. If that mechanism does not exist yet, it is a
separate piece of your infrastructure, not an addition to the notifier.

## References used on this page

- Spot interruption notices: <https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/spot-instance-termination-notices.html>
- Rebalance recommendations: <https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/rebalance-recommendations.html>
- Auto Scaling lifecycle hooks: <https://docs.aws.amazon.com/autoscaling/ec2/userguide/lifecycle-hooks.html>
- Capacity Rebalancing: <https://docs.aws.amazon.com/autoscaling/ec2/userguide/ec2-auto-scaling-capacity-rebalancing.html>
- Mixed instances groups: <https://docs.aws.amazon.com/autoscaling/ec2/userguide/ec2-auto-scaling-mixed-instances-groups.html>
- Launch templates: <https://docs.aws.amazon.com/autoscaling/ec2/userguide/launch-templates.html>
- Tags on groups and instances: <https://docs.aws.amazon.com/autoscaling/ec2/userguide/ec2-auto-scaling-tagging.html>
- User data: <https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/user-data.html>
- Attaching EBS volumes (same AZ): <https://docs.aws.amazon.com/ebs/latest/userguide/ebs-attaching-volume.html>
- EBS Multi-Attach limits: <https://docs.aws.amazon.com/ebs/latest/userguide/ebs-volumes-multi.html>
- Making a volume available, fstab with UUID and nofail: <https://docs.aws.amazon.com/ebs/latest/userguide/ebs-using-volumes.html>
- EBS snapshots: <https://docs.aws.amazon.com/ebs/latest/userguide/ebs-snapshots.html>
- Preserving volumes on termination: <https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/preserving-volumes-on-termination.html>
- Docker daemon data root: <https://docs.docker.com/engine/daemon/>
- Docker volumes: <https://docs.docker.com/engine/storage/volumes/>
- Systems Manager on EC2, tag-restricted Run Command, preinstalled agent:
  <https://docs.aws.amazon.com/systems-manager/latest/userguide/systems-manager-setting-up-ec2.html>,
  <https://docs.aws.amazon.com/systems-manager/latest/userguide/run-command-setting-up.html>,
  <https://docs.aws.amazon.com/systems-manager/latest/userguide/ami-preinstalled-agent.html>
- Coder architecture, networking, Docker template, template tutorial:
  <https://coder.com/docs/admin/infrastructure/architecture>,
  <https://coder.com/docs/admin/networking>,
  <https://registry.coder.com/templates/coder/docker>,
  <https://coder.com/docs/tutorials/template-from-scratch>
