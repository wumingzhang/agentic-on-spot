#!/usr/bin/env bash
# spot-handoff-notify: tell workspace containers on this host that the EC2
# Spot instance is about to be terminated.
#
# The Lambda in this repository ships this file inside an SSM
# AWS-RunShellScript command, so nothing has to be pre-installed on the host
# beyond docker, bash, flock, timeout and mktemp. It can equally be run by hand
# on a host (see README, "Operations").
#
# What it does, per target, all targets in parallel:
#   1. take a per-event, per-target lock under the state dir (flock -n);
#   2. if a .done marker exists for this event+target, report "duplicate";
#   3. otherwise check docker and the container, then run
#        timeout <n> docker exec [-u <user>] <container> <argv...>
#      and write the .done marker only when that exits 0.
# A failed or timed-out target leaves no .done marker, so a redelivery of the
# same event retries exactly the targets that have not succeeded yet.
#
# A --detached-target is started with `docker exec -d` instead: docker
# returns as soon as the process is running in the container, the marker is
# written then, and the target is reported as "launched", not "delivered".
# The timeout bounds that launch only; nothing here waits for, watches or
# ends the detached process, and nothing can tell whether it finished.
# "launched" is deliberately weaker than "delivered".
#
# This is a notification, not a synchronous checkpoint: exit 0 means the
# commands were accepted by the containers, not that any work was saved.
#
# Output: one JSON line per target, then one aggregate line. Exit status is 0
# when no target failed, 1 when any target failed, 2 on usage errors.
set -u
set -o pipefail

usage() {
  cat >&2 <<'USAGE'
usage: spot-handoff-notify.sh --event-id <id> --instance-id <i-...>
         [--deadline <iso8601>] [--state-dir <dir>] [--per-target-timeout <s>]
         [--inspect-timeout <s>] [--retention-days <n>] [--dry-run]
         --target <name> <container> <user|-> <argc> <argv...> [--target ...]
         [--detached-target <name> <container> <user|-> <argc> <argv...>]

  --target   name: [A-Za-z0-9_-]+, unique
             container: exact docker container name
             user: user[:group] or uid[:gid] for docker exec -u, or "-" for the
                   container's default user
             argc: number of argv words that follow (1..64)
  --detached-target  same shape; run with `docker exec -d` and report
             "launched" once docker has started the process, without waiting
             for it to exit. The per-target timeout bounds the launch only.
USAGE
  exit 2
}

die() { printf 'spot-handoff-notify: %s\n' "$*" >&2; exit 2; }

# json_str STR -> prints STR as a JSON string literal. Control characters are
# dropped rather than escaped so a stray terminal sequence from a container
# cannot reach the log.
json_str() {
  local s
  s=$(printf '%s' "$1" | tr -d '\000-\037\177')
  s=${s//\\/\\\\}
  s=${s//\"/\\\"}
  printf '"%s"' "$s"
}

now_s() { date +%s; }

EVENT_ID=""
INSTANCE_ID=""
DEADLINE=""
STATE_DIR="/data/spot-handoff"
PER_TARGET_TIMEOUT=60
INSPECT_TIMEOUT=10
KILL_AFTER=5
RETENTION_DAYS=14
DRY_RUN=0
# Parallel arrays, one entry per target.
T_NAME=()
T_CONTAINER=()
T_USER=()
T_DETACHED=()  # 1 for --detached-target, 0 for --target
T_ARGV=()  # argv words joined by the unit separator, split again in run_target

while [ $# -gt 0 ]; do
  case "$1" in
    --event-id)           [ $# -ge 2 ] || usage; EVENT_ID=$2; shift 2 ;;
    --instance-id)        [ $# -ge 2 ] || usage; INSTANCE_ID=$2; shift 2 ;;
    --deadline)           [ $# -ge 2 ] || usage; DEADLINE=$2; shift 2 ;;
    --state-dir)          [ $# -ge 2 ] || usage; STATE_DIR=$2; shift 2 ;;
    --per-target-timeout) [ $# -ge 2 ] || usage; PER_TARGET_TIMEOUT=$2; shift 2 ;;
    --inspect-timeout)    [ $# -ge 2 ] || usage; INSPECT_TIMEOUT=$2; shift 2 ;;
    --retention-days)     [ $# -ge 2 ] || usage; RETENTION_DAYS=$2; shift 2 ;;
    --dry-run)            DRY_RUN=1; shift ;;
    --target|--detached-target)
      [ $# -ge 5 ] || usage
      detached=0; [ "$1" = "--detached-target" ] && detached=1
      name=$2 container=$3 user=$4 argc=$5
      shift 5
      [[ "$name" =~ ^[A-Za-z0-9_-]{1,64}$ ]] || die "bad target name: $name"
      [[ "$container" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]] || die "bad container name for target $name"
      [[ "$user" == "-" || "$user" =~ ^[A-Za-z0-9_.-]{1,64}(:[A-Za-z0-9_.-]{1,64})?$ ]] || die "bad user for target $name"
      [[ "$argc" =~ ^[1-9][0-9]?$ && "$argc" -le 64 ]] || die "bad argc for target $name"
      [ $# -ge "$argc" ] || die "target $name: expected $argc argv words, got $#"
      for existing in "${T_NAME[@]+"${T_NAME[@]}"}"; do
        [ "$existing" != "$name" ] || die "duplicate target name: $name"
      done
      argv=""
      for ((i = 0; i < argc; i++)); do
        case "$1" in *$'\x1f'*|*$'\n'*) die "target $name: argv word contains a control character" ;; esac
        argv+="$1"$'\x1f'
        shift
      done
      T_NAME+=("$name"); T_CONTAINER+=("$container"); T_USER+=("$user"); T_DETACHED+=("$detached"); T_ARGV+=("$argv")
      ;;
    -h|--help) usage ;;
    *) die "unknown argument: $1" ;;
  esac
done

[[ "$EVENT_ID" =~ ^[A-Za-z0-9._-]{1,128}$ ]] || die "--event-id missing or malformed"
[[ "$INSTANCE_ID" =~ ^i-[0-9a-f]{8}([0-9a-f]{9})?$ ]] || die "--instance-id missing or malformed"
[[ "$PER_TARGET_TIMEOUT" =~ ^[1-9][0-9]{0,3}$ ]] || die "--per-target-timeout must be a positive integer"
[[ "$INSPECT_TIMEOUT" =~ ^[1-9][0-9]{0,3}$ ]] || die "--inspect-timeout must be a positive integer"
[[ "$RETENTION_DAYS" =~ ^[0-9]{1,4}$ ]] || die "--retention-days must be an integer"
[ "${#T_NAME[@]}" -gt 0 ] || die "at least one --target is required"

# State directory. /data is the host's persistent volume (see README), so the
# dedup markers survive a redelivery minutes later. When it is unusable the
# notification still goes out: dedup degrades to a per-boot tmpfs directory
# and the aggregate line says so.
STATE_NOTE=""
if ! mkdir -p "$STATE_DIR/events" 2>/dev/null || [ ! -w "$STATE_DIR/events" ]; then
  fallback="${TMPDIR:-/tmp}/spot-handoff-state"
  STATE_NOTE="state dir $STATE_DIR unusable, using $fallback (dedup not durable)"
  STATE_DIR=$fallback
  mkdir -p "$STATE_DIR/events" 2>/dev/null || die "cannot create state dir $STATE_DIR"
fi
EVENT_DIR="$STATE_DIR/events/$EVENT_ID"
mkdir -p "$EVENT_DIR" || die "cannot create $EVENT_DIR"

# Keep the state bounded: drop event directories older than the retention.
if [ "$RETENTION_DAYS" -gt 0 ]; then
  find "${STATE_DIR:?}/events" -mindepth 1 -maxdepth 1 -type d -mtime +"$RETENTION_DAYS" \
    -exec rm -rf {} + 2>/dev/null || true
fi

# run_target IDX: deliver to one target, print its JSON result line to stdout.
# Runs in a background subshell; everything it needs is in the arrays.
run_target() {
  local idx=$1
  local name=${T_NAME[$idx]} container=${T_CONTAINER[$idx]} user=${T_USER[$idx]} detached=${T_DETACHED[$idx]}
  local -a argv=() cmd=()
  local started rc=0 lock_fd out
  IFS=$'\x1f' read -r -d '' -a argv < <(printf '%s\0' "${T_ARGV[$idx]}")
  started=$(now_s)

  cmd=(docker exec)
  [ "$detached" -eq 0 ] || cmd+=(-d)
  [ "$user" = "-" ] || cmd+=(-u "$user")
  cmd+=("$container" "${argv[@]}")

  emit() {
    # emit STATUS RC DETAIL
    local elapsed=$(( $(now_s) - started ))
    printf '{"type":"target","event_id":%s,"instance_id":%s,"target":%s,"container":%s,"status":%s,"rc":%s,"elapsed_s":%s,"detail":%s}\n' \
      "$(json_str "$EVENT_ID")" "$(json_str "$INSTANCE_ID")" "$(json_str "$name")" \
      "$(json_str "$container")" "$(json_str "$1")" "$2" "$elapsed" "$(json_str "$3")"
    printf '%s %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$3" >>"$EVENT_DIR/$name.result" 2>/dev/null || true
  }

  exec {lock_fd}>"$EVENT_DIR/$name.lock" || { emit failed 1 "cannot open lock file"; return 1; }
  if ! flock -n "$lock_fd"; then
    # Another invocation for the same event is delivering to this target right
    # now. It owns the outcome; we neither duplicate nor fail.
    emit in-progress 0 "another invocation holds the lock for this event and target"
    return 0
  fi
  if [ -e "$EVENT_DIR/$name.done" ]; then
    # The marker says which outcome it records (delivered or launched).
    emit duplicate 0 "already handled: $(head -c 200 "$EVENT_DIR/$name.done" 2>/dev/null)"
    return 0
  fi

  if ! command -v docker >/dev/null 2>&1; then
    emit failed 127 "docker-missing: no docker executable on PATH"
    return 1
  fi

  # docker inspect under its own timeout: a wedged daemon must not eat the
  # whole window before we even try.
  out=$(timeout --kill-after="$KILL_AFTER" "$INSPECT_TIMEOUT" docker inspect -f '{{.State.Running}}' "$container" 2>&1)
  rc=$?
  if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
    emit failed "$rc" "docker-unavailable: docker inspect timed out after ${INSPECT_TIMEOUT}s"
    return 1
  elif [ "$rc" -ne 0 ]; then
    case "$out" in
      *"No such"*) emit failed "$rc" "container-missing: $(printf '%s' "$out" | tail -c 200)" ;;
      *)           emit failed "$rc" "docker-unavailable: $(printf '%s' "$out" | tail -c 200)" ;;
    esac
    return 1
  elif [ "$out" != "true" ]; then
    emit failed 1 "container-not-running: state running=$out"
    return 1
  fi

  if [ "$DRY_RUN" -eq 1 ]; then
    if [ "$detached" -eq 1 ]; then
      emit dry-run 0 "would launch detached (not awaited): $(printf '%q ' "${cmd[@]}")"
    else
      emit dry-run 0 "would run: $(printf '%q ' "${cmd[@]}")"
    fi
    return 0
  fi

  # For a detached target `docker exec -d` returns once the process has been
  # started in the container, so the timeout bounds the launch, and $out
  # holds docker's own output (a start error), never the process's.
  out=$(timeout --kill-after="$KILL_AFTER" "$PER_TARGET_TIMEOUT" "${cmd[@]}" </dev/null 2>&1)
  rc=$?
  printf '%s\n' "$out" | tail -c 4000 >"$EVENT_DIR/$name.log" 2>/dev/null || true
  if [ "$rc" -eq 0 ] && [ "$detached" -eq 1 ]; then
    # The marker records a launch, not a delivery: a redelivery must not
    # start a second process, and a reader must not take it for completion.
    printf '%s launched\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$EVENT_DIR/$name.done.tmp" \
      && mv -f "$EVENT_DIR/$name.done.tmp" "$EVENT_DIR/$name.done"
    emit launched 0 "detached process started; completion not observed${out:+. $(printf '%s' "$out" | tail -c 200)}"
    return 0
  elif [ "$rc" -eq 0 ]; then
    # Atomic marker: a crash between exec and marker leaves no marker, which
    # errs on the side of one more notification rather than none.
    printf '%s delivered\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$EVENT_DIR/$name.done.tmp" \
      && mv -f "$EVENT_DIR/$name.done.tmp" "$EVENT_DIR/$name.done"
    emit delivered 0 "$(printf '%s' "$out" | tail -c 300)"
    return 0
  elif [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
    if [ "$detached" -eq 1 ]; then
      emit failed "$rc" "timeout: launch did not return within ${PER_TARGET_TIMEOUT}s"
    else
      emit failed "$rc" "timeout: command did not finish within ${PER_TARGET_TIMEOUT}s"
    fi
    return 1
  elif [ "$detached" -eq 1 ]; then
    emit failed "$rc" "launch-failed: $(printf '%s' "$out" | tail -c 300)"
    return 1
  else
    emit failed "$rc" "exec-failed: $(printf '%s' "$out" | tail -c 300)"
    return 1
  fi
}

# All targets at once, each in its own subshell, so a hanging container holds
# up only its own timeout and not the others.
declare -a pids=()
RUN_DIR=$(mktemp -d "${TMPDIR:-/tmp}/spot-handoff-run.XXXXXX") || die "mktemp failed"
trap 'rm -rf "${RUN_DIR:?}"' EXIT
for i in "${!T_NAME[@]}"; do
  ( run_target "$i" >"$RUN_DIR/$i.out" 2>"$RUN_DIR/$i.err"; echo $? >"$RUN_DIR/$i.rc" ) &
  pids+=($!)
done
for pid in "${pids[@]}"; do wait "$pid"; done

failed=0 delivered=0 launched=0 duplicate=0 other=0
for i in "${!T_NAME[@]}"; do
  cat "$RUN_DIR/$i.out"
  cat "$RUN_DIR/$i.err" >&2
  line=$(cat "$RUN_DIR/$i.out")
  case "$line" in
    *'"status":"delivered"'*) delivered=$((delivered + 1)) ;;
    *'"status":"launched"'*)  launched=$((launched + 1)) ;;
    *'"status":"duplicate"'*) duplicate=$((duplicate + 1)) ;;
    *'"status":"failed"'*)    failed=$((failed + 1)) ;;
    *) other=$((other + 1)) ;;
  esac
  rc=$(cat "$RUN_DIR/$i.rc" 2>/dev/null || echo 1)
  if [ "$rc" != "0" ] && [[ "$line" != *'"status":"failed"'* ]]; then
    # The subshell died without emitting a line (killed, bash error): count it.
    failed=$((failed + 1)); other=$((other - 1))
    printf '{"type":"target","event_id":%s,"instance_id":%s,"target":%s,"status":"failed","rc":%s,"detail":"no result from target subshell"}\n' \
      "$(json_str "$EVENT_ID")" "$(json_str "$INSTANCE_ID")" "$(json_str "${T_NAME[$i]}")" "$rc"
  fi
done

# "launched" is counted apart from "delivered": a launched target's command
# was started, not seen to finish.
printf '{"type":"summary","event_id":%s,"instance_id":%s,"deadline":%s,"targets":%s,"delivered":%s,"launched":%s,"duplicate":%s,"failed":%s,"other":%s,"dry_run":%s,"state_dir":%s,"note":%s}\n' \
  "$(json_str "$EVENT_ID")" "$(json_str "$INSTANCE_ID")" "$(json_str "$DEADLINE")" \
  "${#T_NAME[@]}" "$delivered" "$launched" "$duplicate" "$failed" "$other" "$DRY_RUN" \
  "$(json_str "$STATE_DIR")" "$(json_str "$STATE_NOTE")"

[ "$failed" -eq 0 ]
