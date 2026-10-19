"""Tests for the host script with a fake `docker` on PATH.

Each test gets its own temp dir holding: bin/docker (the fake), state/ (the
dedup state dir) and exec.log.d/ (one NUL-separated argv record per
`docker exec` the fake received, plus start/end times). The fake is driven by environment variables:

  FAKE_RUNNING   comma-separated container names that `docker inspect`
                 reports as running; others get "No such object"
  FAKE_DAEMON_DOWN=1   every docker call fails like a dead daemon
  FAKE_HANG=<s>  `docker inspect` sleeps this long (wedged daemon)
  FAKE_SLEEP_<NAME>=<s>  `docker exec <container>` sleeps (NAME = container
                 with non-alnum replaced by _ and upper-cased)
  FAKE_RC_<NAME>=<n>     exit status of `docker exec <container>`
  FAKE_BLOCK_FILE=<path> `docker exec` waits until this file exists (lets a
                 test hold a delivery open for as long as it needs)
  FAKE_DETACH_RELEASE_<NAME>=<path>  `docker exec -d <container>` leaves a
                 process behind (the container-side process a real
                 `docker exec -d` would start) that waits until this file
                 exists, then writes a .detached-done record; it gives up
                 after 30 s so a failed test leaves nothing behind

Each exec record starts with `user=<u>` and `detached=<0|1>`, then the
container and the argv words.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import support  # noqa: F401

SCRIPT = Path(support.__file__).resolve().parent.parent / "lambda" / "spot_handoff" / "spot-handoff-notify.sh"
INSTANCE = "i-0123456789abcdef0"

FAKE_DOCKER = r'''#!/usr/bin/env bash
log=${FAKE_LOG:?}
if [ "${FAKE_DAEMON_DOWN:-0}" = "1" ]; then
  echo "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?" >&2
  exit 1
fi
case "$1" in
  inspect)
    [ -n "${FAKE_HANG:-}" ] && sleep "$FAKE_HANG"
    c=${@: -1}
    case ",${FAKE_RUNNING:-}," in
      *",$c,"*) echo true; exit 0 ;;
      *",stopped:$c,"*) echo false; exit 0 ;;
    esac
    echo "Error: No such object: $c" >&2; exit 1 ;;
  exec)
    shift
    user="" detached=0
    while :; do
      case "$1" in
        -d) detached=1; shift ;;
        -u) user=$2; shift 2 ;;
        *) break ;;
      esac
    done
    c=$1
    key=$(printf '%s' "$c" | tr -c 'A-Za-z0-9' '_' | tr 'a-z' 'A-Z')
    # One record file per exec: parallel execs must not interleave writes.
    mkdir -p "$log.d"
    { printf 'user=%s\0' "$user"; printf 'detached=%s\0' "$detached"; printf '%s\0' "$@"; } >"$log.d/$(date +%s%N).$$.rec"
    sleep_var="FAKE_SLEEP_$key"; rc_var="FAKE_RC_$key"; release_var="FAKE_DETACH_RELEASE_$key"
    start=$(date +%s.%N)
    [ -n "${!sleep_var:-}" ] && sleep "${!sleep_var}"
    if [ -n "${FAKE_BLOCK_FILE:-}" ]; then until [ -e "$FAKE_BLOCK_FILE" ]; do sleep 0.05; done; fi
    printf '%s %s %s\n' "$c" "$start" "$(date +%s.%N)" >"$log.d/$(date +%s%N).$$.time"
    if [ "$detached" = 1 ]; then
      # Like the real client: return once the process is started. The process
      # itself lives on in its own session, with no fd back to the launcher,
      # and ends only when the test releases it (not after a fixed sleep, so
      # a slow host cannot make the launcher look like it waited).
      if [ -n "${!release_var:-}" ]; then
        setsid bash -c 'for ((n = 0; n < 600; n++)); do [ -e "$1" ] && break; sleep 0.05; done
                        [ -e "$1" ] && printf "%s\n" "$2" >"$3"' _ "${!release_var}" "$c" "$log.d/$(date +%s%N).$$.detached-done" \
          </dev/null >/dev/null 2>&1 &
      fi
      exit "${!rc_var:-0}"
    fi
    echo "fake exec ok in $c"
    exit "${!rc_var:-0}" ;;
  *) echo "fake docker: unsupported $1" >&2; exit 64 ;;
esac
'''


class HostScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="spot-handoff-test."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.bin / "docker").write_text(FAKE_DOCKER)
        (self.bin / "docker").chmod(0o755)
        self.state = self.tmp / "state"
        self.exec_log = self.tmp / "exec.log"

    def env(self, with_docker=True, **extra):
        system = ":".join(p for p in ("/usr/bin", "/bin") if os.path.isdir(p))
        env = {
            "PATH": (f"{self.bin}:{system}" if with_docker else system),
            "HOME": str(self.tmp),
            "TMPDIR": str(self.tmp),
            "FAKE_LOG": str(self.exec_log),
            "FAKE_RUNNING": "coder-a,coder-b",
        }
        env.update(extra)
        return env

    def run_script(self, *args, event="evt-1", targets=None, env=None, timeout=60, **env_extra):
        if targets is None:
            targets = [("hermes", "coder-a", "-", ["hermes", "-c", "handoff please"]),
                       ("yardr", "coder-b", "coder", ["/home/coder/.local/bin/yardr", "mail", "yardmaster", "x y"])]
        cmd = ["bash", str(SCRIPT), "--event-id", event, "--instance-id", INSTANCE,
               "--state-dir", str(self.state), "--retention-days", "0", *args]
        for name, container, user, argv in targets:
            cmd += ["--target", name, container, user, str(len(argv)), *argv]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              env=env or self.env(**env_extra))
        lines = [json.loads(l) for l in proc.stdout.splitlines() if l.startswith("{")]
        targets_out = {l["target"]: l for l in lines if l.get("type") == "target"}
        summary = next((l for l in lines if l.get("type") == "summary"), None)
        return proc, targets_out, summary

    def execs(self):
        """argv of every `docker exec` the fake received, in start order."""
        rec_dir = Path(str(self.exec_log) + ".d")
        if not rec_dir.is_dir():
            return []
        return [[w.decode() for w in f.read_bytes().split(b"\0") if w] for f in sorted(rec_dir.glob("*.rec"))]

    # --- happy path -------------------------------------------------------

    def test_delivers_to_all_targets_in_parallel_and_exits_zero(self):
        proc, targets, summary = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual({t: v["status"] for t, v in targets.items()}, {"hermes": "delivered", "yardr": "delivered"})
        self.assertEqual((summary["delivered"], summary["failed"], summary["targets"]), (2, 0, 2))
        self.assertEqual(summary["instance_id"], INSTANCE)
        execs = self.execs()
        self.assertEqual(len(execs), 2)
        by_container = {e[2]: e for e in execs}
        self.assertEqual(by_container["coder-a"], ["user=", "detached=0", "coder-a", "hermes", "-c", "handoff please"])
        self.assertEqual(by_container["coder-b"], ["user=coder", "detached=0", "coder-b", "/home/coder/.local/bin/yardr", "mail", "yardmaster", "x y"])
        self.assertTrue((self.state / "events" / "evt-1" / "hermes.done").exists())
        self.assertTrue((self.state / "events" / "evt-1" / "yardr.done").exists())

    def test_argv_with_shell_metacharacters_arrives_verbatim(self):
        nasty = ["sh", "-c", "echo $(id) `id` \"q\" 'q' ; rm -rf / & | *  tab\there"]
        proc, targets, _ = self.run_script(targets=[("t", "coder-a", "-", nasty)])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.execs()[0][3:], nasty)

    def exec_times(self):
        out = {}
        for f in Path(str(self.exec_log) + ".d").glob("*.time"):
            container, start, end = f.read_text().split()
            out[container] = (float(start), float(end))
        return out

    def test_targets_run_concurrently(self):
        # Both execs sleep; under serial execution the second would start only
        # after the first ended. Overlap is independent of machine load.
        proc, targets, _ = self.run_script(FAKE_SLEEP_CODER_A="2", FAKE_SLEEP_CODER_B="2")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        times = self.exec_times()
        (a_start, a_end), (b_start, b_end) = times["coder-a"], times["coder-b"]
        self.assertLess(a_start, b_end)
        self.assertLess(b_start, a_end, "second target started only after the first finished")

    def test_one_hanging_target_is_killed_and_does_not_block_the_other(self):
        # hermes hangs far longer than its timeout; yardr must still be
        # delivered and the whole run must end long before the hang would.
        started = time.monotonic()
        proc, targets, summary = self.run_script("--per-target-timeout", "1", FAKE_SLEEP_CODER_A="40")
        elapsed = time.monotonic() - started
        self.assertEqual(targets["yardr"]["status"], "delivered")
        self.assertEqual(targets["hermes"]["status"], "failed")
        self.assertIn("timeout", targets["hermes"]["detail"])
        self.assertEqual(proc.returncode, 1)
        self.assertLess(elapsed, 30, f"run waited for the hanging target: {elapsed:.1f}s")
        self.assertFalse((self.state / "events" / "evt-1" / "hermes.done").exists())

    # --- duplicates and retries --------------------------------------------

    def test_duplicate_event_is_not_redelivered(self):
        self.run_script()
        proc, targets, summary = self.run_script()
        self.assertEqual(proc.returncode, 0)
        self.assertEqual({v["status"] for v in targets.values()}, {"duplicate"})
        self.assertEqual(summary["duplicate"], 2)
        self.assertEqual(len(self.execs()), 2, "no second docker exec for a duplicate event")

    def test_new_event_id_delivers_again(self):
        self.run_script(event="evt-1")
        proc, targets, _ = self.run_script(event="evt-2")
        self.assertEqual({v["status"] for v in targets.values()}, {"delivered"})
        self.assertEqual(len(self.execs()), 4)

    def test_partial_failure_retries_only_the_failed_target(self):
        proc, targets, summary = self.run_script(FAKE_RC_CODER_B="3")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(targets["hermes"]["status"], "delivered")
        self.assertEqual(targets["yardr"]["status"], "failed")
        self.assertEqual(targets["yardr"]["rc"], 3)
        self.assertIn("exec-failed", targets["yardr"]["detail"])
        self.assertEqual((summary["delivered"], summary["failed"]), (1, 1))
        # Redelivery of the same event: hermes is a duplicate, yardr is retried and now succeeds.
        proc, targets, summary = self.run_script()
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(targets["hermes"]["status"], "duplicate")
        self.assertEqual(targets["yardr"]["status"], "delivered")
        containers = [e[2] for e in self.execs()]
        self.assertEqual(containers.count("coder-a"), 1)
        self.assertEqual(containers.count("coder-b"), 2)

    def test_concurrent_invocations_of_same_event_do_not_double_deliver(self):
        # The first invocation's execs block on a release file, so it holds
        # both locks for as long as the test wants: no sleeps, no races.
        release = self.tmp / "release"
        env = self.env(FAKE_BLOCK_FILE=str(release))
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.run_script, env=env)
            deadline = time.monotonic() + 30
            while len(self.execs()) < 2 and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(len(self.execs()), 2, "first invocation did not start both execs")
            pb, tb, sb = self.run_script(env=env)   # second invocation, while the first is mid-delivery
            release.touch()
            pa, ta, sa = first.result()
        for name in ("hermes", "yardr"):
            self.assertEqual(tb[name]["status"], "in-progress", name)
            self.assertEqual(ta[name]["status"], "delivered", name)
        self.assertEqual((pa.returncode, pb.returncode), (0, 0))
        self.assertEqual(len(self.execs()), 2, "each target exec'd exactly once across both invocations")
        # And once the first has finished, a third invocation sees the markers.
        pc, tc, _ = self.run_script(env=env)
        self.assertEqual({v["status"] for v in tc.values()}, {"duplicate"})

    def test_done_marker_is_atomic_and_describes_delivery(self):
        self.run_script()
        marker = (self.state / "events" / "evt-1" / "hermes.done").read_text()
        self.assertIn("delivered", marker)
        self.assertFalse((self.state / "events" / "evt-1" / "hermes.done.tmp").exists())

    # --- detached targets ---------------------------------------------------
    #
    # A --detached-target is started with `docker exec -d`, reported as
    # "launched" (never "delivered") and deduplicated like any other target.
    # The host never learns whether the process finished.

    DETACHED = [("hermes", "coder-a", "coder", ["/home/coder/.local/bin/hermes", "chat", "--oneshot", "-q", "note please"])]

    def run_detached(self, *args, **kw):
        """One detached target; --detached-target instead of --target."""
        kw.setdefault("targets", [])
        proc, targets, summary = self.run_script(
            *args, "--detached-target", *self.DETACHED[0][:3], str(len(self.DETACHED[0][3])), *self.DETACHED[0][3], **kw)
        return proc, targets, summary

    def detached_done_records(self):
        return list(Path(str(self.exec_log) + ".d").glob("*.detached-done"))

    def test_detached_target_is_launched_with_docker_exec_d_and_not_awaited(self):
        # The fake leaves a process behind that ends only once `release`
        # exists. The script must return with "launched" while that process
        # is still waiting (had it waited for the process, it would hang
        # until run_script's timeout), and the process must still complete
        # once released: nothing waited for it, nothing killed it.
        release = self.tmp / "release"
        self.addCleanup(release.touch)  # a failed assertion must not leave the process waiting
        proc, targets, summary = self.run_detached(FAKE_DETACH_RELEASE_CODER_A=str(release))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(targets["hermes"]["status"], "launched")
        self.assertEqual(targets["hermes"]["rc"], 0)
        self.assertIn("completion not observed", targets["hermes"]["detail"])
        self.assertEqual((summary["launched"], summary["delivered"], summary["failed"], summary["targets"]), (1, 0, 0, 1))
        self.assertEqual(self.execs(), [["user=coder", "detached=1", "coder-a", *self.DETACHED[0][3]]])
        self.assertEqual(self.detached_done_records(), [], "the script waited for the detached process")
        marker = (self.state / "events" / "evt-1" / "hermes.done").read_text()
        self.assertIn("launched", marker)
        self.assertNotIn("delivered", marker)
        release.touch()
        deadline = time.monotonic() + 20
        while not self.detached_done_records() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(len(self.detached_done_records()), 1, "the detached process did not outlive the launch")

    def test_detached_launch_is_deduplicated_per_event(self):
        # Redelivery of the same event: the marker says launched, no second
        # process is started, and the summary counts a duplicate, not a launch.
        self.run_detached()
        proc, targets, summary = self.run_detached()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(targets["hermes"]["status"], "duplicate")
        self.assertIn("launched", targets["hermes"]["detail"])
        self.assertEqual((summary["launched"], summary["duplicate"]), (0, 1))
        self.assertEqual(len(self.execs()), 1, "a redelivery started a second detached process")
        # A different event launches again.
        proc, targets, _ = self.run_detached(event="evt-2")
        self.assertEqual(targets["hermes"]["status"], "launched")
        self.assertEqual(len(self.execs()), 2)

    def test_detached_launch_failure_leaves_no_marker_and_is_retried(self):
        # docker exec -d fails to start the process (no such executable, say):
        # a failure with docker's reason, no marker, and the next delivery of
        # the same event launches again.
        proc, targets, summary = self.run_detached(FAKE_RC_CODER_A="126")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(targets["hermes"]["status"], "failed")
        self.assertEqual(targets["hermes"]["rc"], 126)
        self.assertIn("launch-failed", targets["hermes"]["detail"])
        self.assertEqual((summary["launched"], summary["failed"]), (0, 1))
        self.assertFalse((self.state / "events" / "evt-1" / "hermes.done").exists())
        proc, targets, _ = self.run_detached()
        self.assertEqual(targets["hermes"]["status"], "launched")
        self.assertEqual(len(self.execs()), 2)

    def test_detached_launch_timeout_bounds_the_launch_only(self):
        # A wedged `docker exec -d` (the launch itself hangs) is killed by the
        # per-target timeout like a synchronous exec and reported as a failed
        # launch; the detail names the launch, not the command.
        started = time.monotonic()
        proc, targets, _ = self.run_detached("--per-target-timeout", "1", FAKE_SLEEP_CODER_A="40")
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual(targets["hermes"]["status"], "failed")
        self.assertIn("launch did not return", targets["hermes"]["detail"])
        self.assertFalse((self.state / "events" / "evt-1" / "hermes.done").exists())

    def test_mixed_targets_are_accounted_separately(self):
        # One synchronous and one detached target in the same run: delivered
        # and launched are counted apart, and the exit status is 0 for both.
        detached = self.DETACHED[0]
        proc, targets, summary = self.run_script(
            "--detached-target", *detached[:3], str(len(detached[3])), *detached[3],
            targets=[("yardr", "coder-b", "coder", ["/home/coder/.local/bin/yardr", "mail", "yardmaster", "x"])])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual({t: v["status"] for t, v in targets.items()}, {"hermes": "launched", "yardr": "delivered"})
        self.assertEqual((summary["targets"], summary["delivered"], summary["launched"], summary["duplicate"], summary["failed"]),
                         (2, 1, 1, 0, 0))
        by_container = {e[2]: e for e in self.execs()}
        self.assertEqual(by_container["coder-a"][:2], ["user=coder", "detached=1"])
        self.assertEqual(by_container["coder-b"][:2], ["user=coder", "detached=0"])
        # The synchronous target's failure still fails the run; the launch stands.
        proc, targets, summary = self.run_script(
            "--detached-target", *detached[:3], str(len(detached[3])), *detached[3], event="evt-2",
            targets=[("yardr", "coder-b", "coder", ["yardr"])], FAKE_RC_CODER_B="3")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual({t: v["status"] for t, v in targets.items()}, {"hermes": "launched", "yardr": "failed"})
        self.assertEqual((summary["launched"], summary["failed"]), (1, 1))

    def test_detached_dry_run_shows_the_detached_invocation_and_starts_nothing(self):
        proc, targets, summary = self.run_detached("--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(targets["hermes"]["status"], "dry-run")
        self.assertIn("would launch detached", targets["hermes"]["detail"])
        self.assertIn("docker exec -d -u coder coder-a /home/coder/.local/bin/hermes chat", targets["hermes"]["detail"])
        self.assertEqual(self.execs(), [])
        self.assertEqual(summary["launched"], 0)
        self.assertEqual(list((self.state / "events" / "evt-1").glob("*.done")), [])

    def test_detached_target_shares_the_target_validation(self):
        base = ["bash", str(SCRIPT), "--event-id", "e", "--instance-id", INSTANCE, "--state-dir", str(self.state)]
        cases = {
            "bad argc": base + ["--detached-target", "h", "coder-a", "-", "0"],
            "bad user": base + ["--detached-target", "h", "coder-a", "root;id", "1", "x"],
            "dup name across keywords": base + ["--target", "h", "coder-a", "-", "1", "x", "--detached-target", "h", "coder-b", "-", "1", "x"],
            "too few words": base + ["--detached-target", "h", "coder-a"],
        }
        for label, cmd in cases.items():
            proc = subprocess.run(cmd, capture_output=True, text=True, env=self.env(), timeout=30)
            self.assertEqual(proc.returncode, 2, f"{label}: {proc.stdout} {proc.stderr}")
            self.assertEqual(self.execs(), [], label)

    # --- failure modes ------------------------------------------------------

    def test_missing_container_fails_that_target_only(self):
        proc, targets, summary = self.run_script(targets=[
            ("hermes", "coder-gone", "-", ["hermes"]), ("yardr", "coder-b", "coder", ["yardr"])])
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(targets["hermes"]["status"], "failed")
        self.assertIn("container-missing", targets["hermes"]["detail"])
        self.assertEqual(targets["yardr"]["status"], "delivered")
        self.assertEqual(len(self.execs()), 1)

    def test_stopped_container_is_reported(self):
        proc, targets, _ = self.run_script(targets=[("h", "coder-z", "-", ["hermes"])], FAKE_RUNNING="stopped:coder-z")
        self.assertEqual(targets["h"]["status"], "failed")
        self.assertIn("container-not-running", targets["h"]["detail"])
        self.assertEqual(self.execs(), [])

    def test_docker_daemon_down(self):
        proc, targets, summary = self.run_script(FAKE_DAEMON_DOWN="1")
        self.assertEqual(proc.returncode, 1)
        for t in targets.values():
            self.assertEqual(t["status"], "failed")
            self.assertIn("docker-unavailable", t["detail"])
        self.assertEqual(summary["failed"], 2)

    def test_docker_binary_missing(self):
        # A PATH with every system tool except docker, so a real docker on the
        # test machine cannot stand in for the missing one.
        nodocker = self.tmp / "nodocker"
        nodocker.mkdir()
        for d in ("/usr/bin", "/bin"):
            for entry in os.scandir(d) if os.path.isdir(d) else []:
                if entry.name != "docker" and not (nodocker / entry.name).exists():
                    os.symlink(entry.path, nodocker / entry.name)
        env = self.env(with_docker=False)
        env["PATH"] = str(nodocker)
        proc, targets, _ = self.run_script(env=env)
        self.assertEqual(proc.returncode, 1)
        for t in targets.values():
            self.assertEqual(t["status"], "failed")
            self.assertIn("docker-missing", t["detail"])

    def test_wedged_daemon_inspect_timeout_is_bounded(self):
        started = time.monotonic()
        proc, targets, _ = self.run_script("--inspect-timeout", "1", FAKE_HANG="40")
        self.assertLess(time.monotonic() - started, 30)
        for t in targets.values():
            self.assertEqual(t["status"], "failed")
            self.assertIn("inspect timed out", t["detail"])

    def test_timeout_leaves_no_done_marker(self):
        proc, targets, _ = self.run_script("--per-target-timeout", "1", FAKE_SLEEP_CODER_A="4", FAKE_SLEEP_CODER_B="4")
        self.assertEqual({v["status"] for v in targets.values()}, {"failed"})
        self.assertEqual(list((self.state / "events" / "evt-1").glob("*.done")), [])

    def test_unwritable_state_dir_falls_back_and_still_delivers(self):
        ro = self.tmp / "ro"
        ro.mkdir()
        ro.chmod(0o500)
        self.addCleanup(ro.chmod, 0o700)
        if os.access(ro, os.W_OK):
            self.skipTest("running as root: directory permissions are not enforced")
        cmd = ["bash", str(SCRIPT), "--event-id", "evt-1", "--instance-id", INSTANCE, "--state-dir", str(ro / "state"),
               "--target", "h", "coder-a", "-", "1", "hermes"]
        proc = subprocess.run(cmd, capture_output=True, text=True, env=self.env(), timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        summary = [json.loads(l) for l in proc.stdout.splitlines() if '"summary"' in l][0]
        self.assertIn("not durable", summary["note"])
        self.assertTrue(summary["state_dir"].startswith(str(self.tmp)))
        self.assertEqual(len(self.execs()), 1)

    # --- dry run and argument validation ------------------------------------

    def test_dry_run_prints_command_and_execs_nothing(self):
        proc, targets, summary = self.run_script("--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual({v["status"] for v in targets.values()}, {"dry-run"})
        self.assertIn("docker exec -u coder coder-b", targets["yardr"]["detail"])
        self.assertIn("docker exec coder-a hermes -c", targets["hermes"]["detail"])
        self.assertEqual(self.execs(), [])
        self.assertEqual(summary["dry_run"], 1)
        self.assertEqual(list((self.state / "events" / "evt-1").glob("*.done")), [])
        # A real run after a dry run still delivers: dry runs leave no marker.
        proc, targets, _ = self.run_script()
        self.assertEqual({v["status"] for v in targets.values()}, {"delivered"})

    def test_dry_run_still_checks_the_container(self):
        proc, targets, _ = self.run_script("--dry-run", targets=[("h", "coder-gone", "-", ["hermes"])])
        self.assertEqual(targets["h"]["status"], "failed")

    def test_rejects_bad_arguments(self):
        base = ["bash", str(SCRIPT), "--instance-id", INSTANCE, "--state-dir", str(self.state)]
        good_target = ["--target", "h", "coder-a", "-", "1", "hermes"]
        cases = {
            "no event id": base + good_target,
            "bad event id": base + ["--event-id", "e;rm", *good_target],
            "bad instance id": ["bash", str(SCRIPT), "--event-id", "e", "--instance-id", "i-nope", *good_target],
            "no targets": base + ["--event-id", "e"],
            "bad container": base + ["--event-id", "e", "--target", "h", "bad name", "-", "1", "x"],
            "bad user": base + ["--event-id", "e", "--target", "h", "coder-a", "root;id", "1", "x"],
            "bad argc": base + ["--event-id", "e", "--target", "h", "coder-a", "-", "0"],
            "short argv": base + ["--event-id", "e", "--target", "h", "coder-a", "-", "3", "x"],
            "dup names": base + ["--event-id", "e", *good_target, *good_target],
            "unknown flag": base + ["--event-id", "e", "--bogus", *good_target],
            "newline in argv": base + ["--event-id", "e", "--target", "h", "coder-a", "-", "1", "a\nb"],
            "bad timeout": base + ["--event-id", "e", "--per-target-timeout", "lots", *good_target],
        }
        for label, cmd in cases.items():
            proc = subprocess.run(cmd, capture_output=True, text=True, env=self.env(), timeout=30)
            self.assertEqual(proc.returncode, 2, f"{label}: {proc.stdout} {proc.stderr}")
            self.assertEqual(self.execs(), [], label)

    def test_output_is_json_lines_only(self):
        proc, _, _ = self.run_script(FAKE_RC_CODER_B="1")
        for line in proc.stdout.splitlines():
            json.loads(line)

    def test_json_escaping_of_container_output(self):
        # The fake echoes the container name into its output; a name cannot carry
        # quotes, so inject them via the exec'd argv echoed back by `sh -c`.
        (self.bin / "docker").write_text(FAKE_DOCKER.replace('echo "fake exec ok in $c"',
                                                             'printf \'he said "hi"\\\\ \\033[1mbold\\n\''))
        proc, targets, _ = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(targets["hermes"]["detail"], 'he said "hi"\\ [1mbold')


if __name__ == "__main__":
    unittest.main()
