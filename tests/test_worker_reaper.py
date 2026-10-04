"""Real Node helper with only isolated test processes, no WorkBuddy or models."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import unittest

from workbuddy_bridge_intelmac.owned_runtime import LAUNCHER


NODE = shutil.which("node")
REAPER = LAUNCHER.with_name("worker_reaper.cjs")
LEADER = """
import json, os, signal, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(30)"], stdout=subprocess.PIPE, text=True)
child.stdout.readline()
reaper = subprocess.Popen([sys.argv[1], sys.argv[2], '11111111-2222-4333-8444-555555555555', str(os.getpid()), sys.argv[3]], start_new_session=sys.argv[4]=='wrong', stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={})
print(json.dumps({'child':child.pid,'reaper':reaper.pid}),flush=True)
time.sleep(30)
"""


def alive(pid):
    result = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True, timeout=2)
    return result.returncode == 0 and bool(result.stdout.strip()) and not result.stdout.strip().startswith("Z")


@unittest.skipUnless(NODE and os.name == "posix", "Node.js and POSIX process groups required")
class ReaperTests(unittest.TestCase):
    def run_case(self, mode):
        owner = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
        leader = subprocess.Popen([sys.executable, "-c", LEADER, NODE, str(REAPER), str(owner.pid), mode],
                                  start_new_session=True, stdout=subprocess.PIPE, text=True)
        identities = {}
        try:
            identities = json.loads(leader.stdout.readline())
            time.sleep(0.4)  # helper has validated the initial parent/group
            if mode == "wrong":
                self.assertFalse(alive(identities["reaper"]))
                self.assertTrue(alive(leader.pid))
                self.assertTrue(alive(identities["child"]))
                return
            target = owner if mode == "owner" else leader
            target.kill()
            target.wait(timeout=2)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and any(alive(pid) for pid in identities.values()):
                time.sleep(0.1)
            self.assertFalse(alive(identities["child"]), "TERM-resistant child survived")
            self.assertFalse(alive(identities["reaper"]), "reaper retained its process group")
            self.assertFalse(alive(leader.pid))
        finally:
            # This group was created by this test's leader, never the test runner.
            remaining = [pid for pid in [leader.pid, *identities.values()] if alive(pid)]
            if remaining:
                try:
                    if any(os.getpgid(pid) == leader.pid for pid in remaining):
                        os.killpg(leader.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            owner.kill() if owner.poll() is None else None
            owner.wait(timeout=2)
            leader.wait(timeout=2)
            leader.stdout.close()

    def test_owner_death_removes_term_resistant_descendant(self):
        self.run_case("owner")

    def test_leader_death_removes_term_resistant_descendant(self):
        self.run_case("leader")

    def test_wrong_group_exits_without_signalling_parent_or_child(self):
        self.run_case("wrong")


if __name__ == "__main__":
    unittest.main()
