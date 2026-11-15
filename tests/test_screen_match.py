import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build


class ScreenHasTest(unittest.TestCase):
    """_screen_has() is what waitForText and start_and_wait match with."""

    def setUp(self):
        self._saved = os.environ.get("VM_OCR")

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("VM_OCR", None)
        else:
            os.environ["VM_OCR"] = self._saved

    def ocr(self, mode):
        if mode is None:
            os.environ.pop("VM_OCR", None)
        else:
            os.environ["VM_OCR"] = mode

    def test_exact_match(self):
        for mode in (None, "py", "paddle"):
            self.ocr(mode)
            self.assertTrue(build._screen_has(
                "console login", "omnios console login:"))

    def test_paddle_reads_g_as_q(self):
        # The screen omnios-builder job 125788318078 read at the login prompt.
        self.ocr("paddle")
        self.assertTrue(build._screen_has(
            "console login", "omnios console loqin:"))
        self.assertTrue(build._screen_has(
            "onfigure", "Confiqure Networking"))

    def test_only_paddle_folds(self):
        for mode in (None, "py"):
            self.ocr(mode)
            self.assertFalse(build._screen_has(
                "console login", "omnios console loqin:"))

    def test_other_text_does_not_match(self):
        self.ocr("paddle")
        self.assertFalse(build._screen_has(
            "console login", "Configuring devices.\nHostname: omnios"))
        self.assertFalse(build._screen_has("console login", ""))


class LoginWaitTest(unittest.TestCase):
    """Both login checks go through _screen_has: waitForText's poll and
    start_and_wait's re-check of the screen after it."""

    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)  # waitForText writes _screenText.txt

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def test_paddle_loqin_counts_as_the_login_prompt(self):
        envs = {"VM_OS_NAME": "omnios", "VM_OCR": "paddle",
                "VM_LOGIN_TAG": "console login", "VM_LOGIN_MAX_SECONDS": "30",
                "VM_USE_CONSOLE_BUILD": ""}
        with mock.patch.dict(os.environ, envs), \
                mock.patch.object(build.time, "sleep"), \
                mock.patch.object(build, "log"), \
                mock.patch.object(build, "_unseen_screen_text", return_value=""), \
                mock.patch.object(build, "_screen_text_value",
                                  return_value="omnios console loqin:"), \
                mock.patch.object(build, "startVM", return_value=0), \
                mock.patch.object(build, "openConsole", return_value=0), \
                mock.patch.object(build, "run_hook", return_value=False), \
                mock.patch.object(build, "destroyVM") as destroy:
            self.assertEqual(build.waitForText("console login", "30"), 0)
            self.assertEqual(build.start_and_wait(), 0)
            destroy.assert_not_called()


class FreshLinesTest(unittest.TestCase):

    def test_new_lines_only(self):
        seen = set()
        self.assertEqual(build._fresh_lines("Loading kernel\nBooting", seen),
                         "Loading kernel\nBooting")
        self.assertEqual(build._fresh_lines("Booting\nomnios console login:",
                                            seen),
                         "omnios console login:")
        self.assertEqual(build._fresh_lines("omnios console login:", seen), "")

    def test_whitespace_jitter_is_not_new(self):
        seen = set()
        build._fresh_lines("Hostname:  omnios", seen)
        self.assertEqual(build._fresh_lines(" Hostname: omnios \n\n", seen), "")

    def test_unseen_screen_text_still_dedupes_per_process(self):
        saved = os.environ.pop("VM_USE_CONSOLE_BUILD", None)
        saved_seen = build._screen_dump_state["ocr_seen"]
        build._screen_dump_state["ocr_seen"] = set()
        try:
            self.assertEqual(build._unseen_screen_text("a\nb"), "a\nb")
            self.assertEqual(build._unseen_screen_text("b\nc"), "c")
        finally:
            build._screen_dump_state["ocr_seen"] = saved_seen
            if saved is not None:
                os.environ["VM_USE_CONSOLE_BUILD"] = saved


class WebScreenSinceTest(unittest.TestCase):

    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def write(self, text, mtime):
        with open("screen.txt", "w") as f:
            f.write(text)
        os.utime("screen.txt", (mtime, mtime))

    def test_no_file(self):
        self.assertEqual(build._web_screen_since(time.time()), "")

    def test_file_from_before_this_boot_is_ignored(self):
        t0 = time.time()
        self.write("Shutdown started.", t0 - 60)
        self.assertEqual(build._web_screen_since(t0), "")

    def test_file_written_during_this_boot(self):
        t0 = time.time()
        self.write("omnios console login:", t0 + 5)
        self.assertEqual(build._web_screen_since(t0), "omnios console login:")


if __name__ == "__main__":
    unittest.main()
