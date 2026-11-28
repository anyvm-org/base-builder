import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build


class FakeSsh(object):
    """Stands in for build._ssh_run: records every call and answers with the
    next queued (returncode, stdout, timed_out) tuple, default (0, b"", False)."""

    def __init__(self, answers=None):
        self.calls = []
        self.answers = list(answers or [])

    def __call__(self, argv, timeout=None, **kw):
        stdin = kw.get("stdin")
        self.calls.append({"argv": argv, "timeout": timeout, "kw": kw,
                           "stdin_name": getattr(stdin, "name", None)})
        rc, out, timed_out = self.answers.pop(0) if self.answers else (0, b"", False)
        cp = subprocess.CompletedProcess(argv, rc, out, b"")
        if timed_out:
            cp.timed_out = True
        return cp


class LogCapture(object):
    def __init__(self):
        self.lines = []

    def __call__(self, msg):
        self.lines.append(str(msg))

    def has(self, text):
        return any(text in l for l in self.lines)


class SshRunTest(unittest.TestCase):
    def setUp(self):
        self.log = LogCapture()
        p = mock.patch.object(build, "log", self.log)
        p.start()
        self.addCleanup(p.stop)

    def test_timeout_is_marked(self):
        exc = subprocess.TimeoutExpired(["ssh", "vm", "sh"], 5, output=b"partial")
        with mock.patch.object(build.subprocess, "run", side_effect=exc):
            cp = build._ssh_run(["ssh", "vm", "sh"], timeout=5)
        self.assertEqual(cp.returncode, 124)
        self.assertTrue(cp.timed_out)
        self.assertEqual(cp.stdout, b"partial")
        self.assertTrue(self.log.has("ssh command exceeded 5 s and was killed: vm sh"))

    def test_timeout_without_captured_streams_returns_empty_bytes(self):
        argv = ["ssh", "freebsd", "sh"]
        with mock.patch.object(build.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(argv, 5)):
            cp = build._ssh_run(argv, timeout=5)
        self.assertEqual((cp.returncode, cp.timed_out, cp.stdout, cp.stderr),
                         (124, True, b"", b""))

    def test_real_child_with_file_stdin_is_killed(self):
        # A real process, as run_hook drives it: stdin is the hook file.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "vm_finalize.sh")
            with open(path, "wb") as script:
                script.write(b"echo hi\n")
            child = ("import sys, time\n"
                     "assert sys.stdin.buffer.read() == b'echo hi\\n'\n"
                     "print('script-read', flush=True)\n"
                     "time.sleep(30)\n")
            with open(path, "rb") as source:
                cp = build._ssh_run([sys.executable, "-c", child], timeout=2,
                                    stdin=source, capture_output=True)
            self.assertTrue(source.closed)
        self.assertEqual(cp.returncode, 124)
        self.assertTrue(cp.timed_out)
        self.assertIn(b"script-read", cp.stdout)

    def test_normal_run_is_passed_through_unmarked(self):
        done = subprocess.CompletedProcess(["ssh", "vm", "true"], 124, b"", b"")
        with mock.patch.object(build.subprocess, "run", return_value=done) as run:
            cp = build._ssh_run(["ssh", "vm", "true"], capture_output=True)
        self.assertIs(cp, done)
        self.assertFalse(getattr(cp, "timed_out", False))
        run.assert_called_once_with(["ssh", "vm", "true"],
                                    timeout=build.SSH_CMD_TIMEOUT,
                                    capture_output=True)


class GuestScriptTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        old = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, old)
        os.mkdir("hooks")
        self.log = LogCapture()
        for target, value in ((build, "log"), ):
            p = mock.patch.object(target, value, self.log)
            p.start()
            self.addCleanup(p.stop)
        envp = mock.patch.dict(os.environ, {"VM_OS_NAME": "testos"})
        envp.start()
        self.addCleanup(envp.stop)
        for var in ("VM_GUEST_HOOK_MAX_SECONDS", "VM_EXTRA_SCRIPT_MAX_SECONDS"):
            os.environ.pop(var, None)
        self.assertNotIn("osname", build.__dict__)

    def write(self, path, body="echo hi\n"):
        with open(path, "w") as f:
            f.write(body)

    def fake(self, answers=None):
        fk = FakeSsh(answers)
        p = mock.patch.object(build, "_ssh_run", fk)
        p.start()
        self.addCleanup(p.stop)
        return fk


class RunHookGuestTest(GuestScriptTestBase):
    def test_postbuild_runs_bounded(self):
        self.write("hooks/vm_postBuild.sh")
        fk = self.fake()
        self.assertTrue(build.run_hook("postBuild"))
        self.assertEqual(len(fk.calls), 1)
        call = fk.calls[0]
        self.assertEqual(call["argv"], ["ssh", "-o", "SendEnv=VM_RELEASE", "testos", "sh"])
        self.assertEqual(call["timeout"], 5400)
        self.assertEqual(call["timeout"], build.GUEST_HOOK_MAX_SECONDS["postBuild"])
        self.assertTrue(call["stdin_name"].endswith("vm_postBuild.sh"))
        self.assertFalse(self.log.has("exited"))

    def test_finalize_ceiling(self):
        self.write("hooks/vm_finalize.sh")
        fk = self.fake()
        build.run_hook("finalize")
        self.assertEqual(fk.calls[0]["timeout"], 2700)

    def test_other_hook_point_gets_default(self):
        self.write("hooks/vm_enablessh.sh")
        fk = self.fake()
        build.run_hook("enablessh")
        self.assertEqual(fk.calls[0]["timeout"], 5400)
        self.assertEqual(fk.calls[0]["timeout"], build.GUEST_HOOK_DEFAULT_MAX_SECONDS)

    def run_guest_and_host(self, answer):
        """vm_custom.sh + host_custom.sh with the pipeline's osname global
        set; returns (ssh calls, host bash calls, result or SystemExit)."""
        self.write("hooks/vm_custom.sh", "exit 0\n")
        self.write("hooks/host_custom.sh", "exit 0\n")
        fk = self.fake([answer])
        host_calls = []

        def fake_run(argv, **kw):
            host_calls.append(argv)
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(build, "osname", "pipeline-os", create=True), \
                mock.patch.object(build.subprocess, "run", fake_run):
            try:
                result = build.run_hook("custom")
            except SystemExit as e:
                result = e
        return fk.calls, host_calls, result

    def test_guest_exit_124_still_runs_host_hook(self):
        calls, host_calls, result = self.run_guest_and_host((124, b"", False))
        self.assertEqual(calls[0]["argv"][-2], "pipeline-os")
        self.assertEqual(calls[0]["timeout"], 5400)
        self.assertEqual(host_calls, [["bash", "hooks/host_custom.sh"]])
        self.assertTrue(result)

    def test_guest_timeout_skips_host_hook(self):
        calls, host_calls, result = self.run_guest_and_host((124, b"", True))
        self.assertIsInstance(result, SystemExit)
        self.assertEqual(result.code, 1)
        self.assertEqual(host_calls, [])

    def test_conf_override(self):
        self.write("hooks/vm_finalize.sh")
        os.environ["VM_GUEST_HOOK_MAX_SECONDS"] = "77"
        fk = self.fake()
        build.run_hook("finalize")
        self.assertEqual(fk.calls[0]["timeout"], 77)

    def test_timeout_fails_the_build(self):
        self.write("hooks/vm_finalize.sh")
        self.fake([(124, b"", True)])
        with self.assertRaises(SystemExit) as cm:
            build.run_hook("finalize")
        self.assertEqual(cm.exception.code, 1)
        self.assertTrue(self.log.has(
            "FATAL: guest hook hooks/vm_finalize.sh did not finish within 2700 s"))

    def test_guest_exit_124_is_not_a_timeout(self):
        self.write("hooks/vm_finalize.sh")
        self.fake([(124, b"", False)])
        self.assertTrue(build.run_hook("finalize"))
        self.assertTrue(self.log.has("guest hook hooks/vm_finalize.sh exited 124"))
        self.assertFalse(self.log.has("FATAL"))

    def test_nonzero_exit_is_logged_not_fatal(self):
        self.write("hooks/vm_postBuild.sh")
        self.fake([(255, b"", False)])
        self.assertTrue(build.run_hook("postBuild"))
        self.assertTrue(self.log.has("guest hook hooks/vm_postBuild.sh exited 255"))

    def test_no_guest_hook_no_ssh(self):
        fk = self.fake()
        self.assertFalse(build.run_hook("postBuild"))
        self.assertEqual(fk.calls, [])


class ExtraScriptTest(GuestScriptTestBase):
    def test_bounded_with_keepalive(self):
        self.write("hooks/xfce.sh")
        fk = self.fake([(3, b"", False)])
        self.assertEqual(build._run_extra_script("hooks/xfce.sh", "testos"), 3)
        call = fk.calls[0]
        self.assertEqual(call["argv"], ["ssh", "-o", "SendEnv=VM_RELEASE",
                                        "-o", "ServerAliveInterval=30",
                                        "-o", "ServerAliveCountMax=20",
                                        "testos", "sh"])
        self.assertEqual(call["timeout"], 2700)
        self.assertEqual(call["timeout"], build.EXTRA_SCRIPT_MAX_SECONDS)
        self.assertTrue(call["stdin_name"].endswith("xfce.sh"))

    def test_conf_override(self):
        self.write("hooks/xfce.sh")
        os.environ["VM_EXTRA_SCRIPT_MAX_SECONDS"] = "99"
        fk = self.fake()
        self.assertEqual(build._run_extra_script("hooks/xfce.sh", "testos"), 0)
        self.assertEqual(fk.calls[0]["timeout"], 99)

    def test_timeout_returns_124(self):
        self.write("hooks/xfce.sh")
        self.fake([(124, b"", True)])
        self.assertEqual(build._run_extra_script("hooks/xfce.sh", "testos"), 124)


class DiagnosticsTest(GuestScriptTestBase):
    def setUp(self):
        super(DiagnosticsTest, self).setUp()
        for name in ("sh", ):
            p = mock.patch.object(build, name, lambda *a, **k: 0)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(build.subprocess, "call",
                              side_effect=AssertionError("unbounded ssh"))
        p.start()
        self.addCleanup(p.stop)

    def test_send_env_ok(self):
        fk = self.fake([(0, b"GITHUB_ANYVM=1\n", False)])
        self.assertTrue(build._send_env_check())
        self.assertEqual(len(fk.calls), 1)
        call = fk.calls[0]
        self.assertEqual(call["argv"], ["ssh", "testos", "sh", "-c", "env"])
        self.assertIsNone(call["timeout"])
        self.assertTrue(call["kw"]["capture_output"])
        self.assertEqual(call["kw"]["env"]["GITHUB_ANYVM"], "1")

    def test_send_env_broken_runs_bounded_diagnostics(self):
        fk = self.fake([(0, b"PATH=/bin\n", False)])
        self.assertFalse(build._send_env_check())
        self.assertEqual([c["argv"][2:] for c in fk.calls[1:]],
                         [["sh", "-c", "env"], ["ls -lah"], ["ls -lah .ssh"],
                          ["cat .ssh/*"], ["cat /etc/ssh/sshd_config"]])
        self.assertTrue(all(c["timeout"] is None for c in fk.calls))

    def test_send_env_timed_out_probe_fails_without_guest_dumps(self):
        # Partial output that does carry the marker still is not a pass.
        fk = self.fake([(124, b"GITHUB_ANYVM=1\n", True)])
        self.assertFalse(build._send_env_check())
        self.assertEqual(len(fk.calls), 1)
        self.assertTrue(self.log.has("SendEnv check: the guest did not answer"))
        self.assertFalse(self.log.has("SendEnv OK"))

    def test_show_guest_home(self):
        fk = self.fake([(0, b"", False), (2, b"", False), (1, b"", False)])
        self.assertTrue(build._show_guest_home("freebsd"))
        self.assertEqual([c["argv"] for c in fk.calls],
                         [["ssh", "freebsd", "mkdir -p $HOME/work"],
                          ["ssh", "freebsd", "ls -lah $HOME"],
                          ["ssh", "freebsd", "cat /etc/ssh/sshd_config"]])
        self.assertTrue(all(c["timeout"] is None for c in fk.calls))
        self.assertTrue(self.log.has("======Show ssh config: "))

    def test_show_guest_home_haiku(self):
        fk = self.fake()
        self.assertTrue(build._show_guest_home("haiku"))
        self.assertEqual([c["argv"] for c in fk.calls],
                         [["ssh", "haiku", "mkdir -p '$HOME/work'"],
                          ["ssh", "haiku", "ls -lah '$HOME'"],
                          ["ssh", "haiku", "cat /boot/system/settings/ssh/sshd_config"]])

    def test_show_guest_home_stops_at_first_timeout(self):
        fk = self.fake([(124, b"", True)])
        self.assertFalse(build._show_guest_home("freebsd"))
        self.assertEqual(len(fk.calls), 1)

    def test_show_guest_home_sshd_config_timeout(self):
        fk = self.fake([(0, b"", False), (0, b"", False), (124, b"", True)])
        self.assertFalse(build._show_guest_home("freebsd"))
        self.assertEqual(len(fk.calls), 3)


if __name__ == "__main__":
    unittest.main()
