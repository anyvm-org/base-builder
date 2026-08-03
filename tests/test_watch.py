import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import watch


def write(path, content, mode=None):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    if mode is not None:
        os.chmod(path, mode)


def conf_text(os_name, release, arch="", url=None, extra=""):
    lines = ['VM_OS_NAME="%s"' % os_name, "VM_RELEASE=%s" % release]
    if arch:
        lines.append("VM_ARCH=%s" % arch)
    if url:
        lines.append('VM_VHD_LINK="%s"' % url)
    if extra:
        lines.append(extra)
    lines.append('VM_SYNC_METHODS="rsync,scp"')
    return "\n".join(lines) + "\n"


class WatchCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        old = os.getcwd()
        self.addCleanup(os.chdir, old)
        os.chdir(tmp.name)
        os.makedirs("conf")

    def add(self, name, content):
        write(os.path.join("conf", name), content)

    def hook(self, body):
        write(watch.HOOK_PATH, body + "\n", 0o755)


class TestDetect(WatchCase):
    def test_prints_version(self):
        self.hook('print("15.2")')
        self.assertEqual(watch.detect_upstream(), ("15.2", 0))

    def test_empty_output_is_noop_not_error(self):
        self.hook('pass')
        self.assertEqual(watch.detect_upstream(), ("", 0))

    def test_nonzero_exit_is_reported(self):
        self.hook('import sys; sys.stderr.write("boom\\n"); sys.exit(3)')
        version, rc = watch.detect_upstream()
        self.assertEqual(version, "")
        self.assertNotEqual(rc, 0)

    def test_missing_hook(self):
        self.assertEqual(watch.detect_upstream(), ("", watch.NO_HOOK))

    def test_vm_vars_are_stripped_but_the_environment_survives(self):
        self.hook('import os\n'
                  'print("%s|%s" % (os.environ.get("VM_RELEASE", "-"),\n'
                  '                 os.environ.get("WATCH_PROBE", "-")))')
        os.environ["VM_RELEASE"] = "leaked"
        os.environ["WATCH_PROBE"] = "kept"
        self.addCleanup(os.environ.pop, "VM_RELEASE", None)
        self.addCleanup(os.environ.pop, "WATCH_PROBE", None)
        # VM_* must not steer detection; everything else must reach the
        # hook -- an HTTPS fetch needs platform variables to work at all.
        self.assertEqual(watch.detect_upstream(), ("-|kept", 0))


class TestDecide(WatchCase):
    def _load(self):
        os_name, entries = __import__("gendata").scan_confs()
        notes = __import__("gendata").parse_notes()
        return os_name, entries, notes

    def test_new_version_picks_newest_as_template(self):
        self.add("demo-15.0.conf", conf_text("demo", "15.0"))
        self.add("demo-15.1.conf", conf_text("demo", "15.1"))
        os_name, entries, notes = self._load()
        self.assertEqual(watch.decide(os_name, entries, notes, "15.2"),
                         ("new", "15.1"))

    def test_existing_version_is_noop(self):
        self.add("demo-15.1.conf", conf_text("demo", "15.1"))
        os_name, entries, notes = self._load()
        self.assertEqual(watch.decide(os_name, entries, notes, "15.1")[0],
                         "none")

    def test_older_version_is_noop(self):
        self.add("demo-15.1.conf", conf_text("demo", "15.1"))
        os_name, entries, notes = self._load()
        self.assertEqual(watch.decide(os_name, entries, notes, "9.4")[0],
                         "none")

    def test_shelved_version_is_noop(self):
        self.add("demo-15.1.conf", conf_text("demo", "15.1"))
        write(__import__("gendata").NOTES_PATH,
              "<!-- shelved: 16.0 -->\n")
        os_name, entries, notes = self._load()
        self.assertEqual(watch.decide(os_name, entries, notes, "16.0")[0],
                         "none")

    def test_desktop_variant_never_becomes_the_template(self):
        self.add("demo-15.1.conf", conf_text("demo", "15.1"))
        self.add("demo-15.1-xfce.conf",
                 conf_text("demo", "15.1-xfce",
                           extra='VM_EXTRA_SCRIPT="hooks/xfce.sh"'))
        os_name, entries, notes = self._load()
        self.assertEqual(watch.decide(os_name, entries, notes, "15.2"),
                         ("new", "15.1"))

    def test_shelved_variant_does_not_suppress_the_release(self):
        self.add("demo-15.1.conf", conf_text("demo", "15.1"))
        write(__import__("gendata").NOTES_PATH,
              "<!-- shelved: 16.0-aarch64 -->\n")
        os_name, entries, notes = self._load()
        # only the aarch64 variant is hidden; the 16.0 release itself is
        # still wanted, and plan_files() skips the shelved variant.
        self.assertEqual(watch.decide(os_name, entries, notes, "16.0"),
                         ("new", "15.1"))

    def test_shelved_newest_is_not_used_as_template(self):
        self.add("demo-15.0.conf", conf_text("demo", "15.0"))
        self.add("demo-15.1.conf", conf_text("demo", "15.1"))
        write(__import__("gendata").NOTES_PATH, "<!-- shelved: 15.1 -->\n")
        os_name, entries, notes = self._load()
        self.assertEqual(watch.decide(os_name, entries, notes, "15.2"),
                         ("new", "15.0"))

    def test_build_variant_never_becomes_the_template(self):
        # omnios/openindiana shape: the -build image is an ordinary conf
        # (no VM_EXTRA_SCRIPT), so gendata sees "r151058-build" as a
        # release of its own -- and it sorts ABOVE "r151058".
        self.add("demo-r151058.conf", conf_text("demo", "r151058"))
        self.add("demo-r151058-build.conf",
                 conf_text("demo", "r151058-build"))
        os_name, entries, notes = self._load()
        self.assertEqual(watch.decide(os_name, entries, notes, "r151060"),
                         ("new", "r151058"))

    def test_separate_iso_desktop_never_becomes_the_template(self):
        # ghostbsd shape: each desktop is a different live ISO, so the
        # variant confs set no VM_EXTRA_SCRIPT either.
        self.add("demo-26.1.conf", conf_text("demo", "26.1"))
        self.add("demo-26.1-xfce.conf", conf_text("demo", "26.1-xfce"))
        self.add("demo-26.1-gershwin.conf",
                 conf_text("demo", "26.1-gershwin"))
        os_name, entries, notes = self._load()
        self.assertEqual(watch.decide(os_name, entries, notes, "26.2"),
                         ("new", "26.1"))

    def test_chained_toolchain_variants_resolve_to_the_real_release(self):
        # solaris shape: 11.4-gcc-14 extends 11.4-gcc, which extends 11.4.
        self.add("demo-11.4.conf", conf_text("demo", "11.4"))
        self.add("demo-11.4-gcc.conf", conf_text("demo", "11.4-gcc"))
        self.add("demo-11.4-gcc-14.conf", conf_text("demo", "11.4-gcc-14"))
        os_name, entries, notes = self._load()
        self.assertEqual(watch.decide(os_name, entries, notes, "11.5"),
                         ("new", "11.4"))

    def test_suffixed_release_name_is_not_mistaken_for_a_variant(self):
        # openEuler shape: "24.03-LTS-SP1" LOOKS like a variant but does
        # not extend any other conf's release -- there is no bare "24.03".
        self.add("demo-22.03-LTS-SP4.conf",
                 conf_text("demo", "22.03-LTS-SP4"))
        self.add("demo-24.03-LTS-SP1.conf",
                 conf_text("demo", "24.03-LTS-SP1"))
        os_name, entries, notes = self._load()
        self.assertEqual(
            watch.decide(os_name, entries, notes, "24.03-LTS-SP2"),
            ("new", "24.03-LTS-SP1"))


class TestRealBases(WatchCase):
    """The variant-vs-release rule, exercised directly."""

    def _entries(self):
        return __import__("gendata").scan_confs()[1]

    def test_plain_releases_are_all_real(self):
        self.add("demo-15.0.conf", conf_text("demo", "15.0"))
        self.add("demo-15.1.conf", conf_text("demo", "15.1"))
        self.assertEqual(watch.real_bases(self._entries()),
                         set(["15.0", "15.1"]))

    def test_hyphen_extension_is_a_variant(self):
        self.add("demo-r1.conf", conf_text("demo", "r1"))
        self.add("demo-r1-build.conf", conf_text("demo", "r1-build"))
        entries = self._entries()
        self.assertEqual(watch.real_bases(entries), set(["r1"]))
        by_tag = dict((e["tag"], e) for e in entries)
        self.assertEqual(
            watch.watch_base(by_tag["r1-build"], set(["r1"])), "r1")

    def test_chain_collapses_to_the_bottom(self):
        self.add("demo-11.4.conf", conf_text("demo", "11.4"))
        self.add("demo-11.4-gcc.conf", conf_text("demo", "11.4-gcc"))
        self.add("demo-11.4-gcc-14.conf", conf_text("demo", "11.4-gcc-14"))
        entries = self._entries()
        bases = watch.real_bases(entries)
        self.assertEqual(bases, set(["11.4"]))
        by_tag = dict((e["tag"], e) for e in entries)
        self.assertEqual(watch.watch_base(by_tag["11.4-gcc-14"], bases),
                         "11.4")

    def test_arch_only_release_is_still_real(self):
        # a release that exists only as a non-x86_64 conf: VM_RELEASE is
        # the bare version, the arch lives in the filename
        self.add("demo-16.0-aarch64.conf",
                 conf_text("demo", "16.0", arch="aarch64"))
        self.assertEqual(watch.real_bases(self._entries()), set(["16.0"]))

    def test_lookalike_suffix_names_stay_real(self):
        self.add("demo-22.03-LTS-SP4.conf", conf_text("demo", "22.03-LTS-SP4"))
        self.add("demo-24.03-LTS-SP1.conf", conf_text("demo", "24.03-LTS-SP1"))
        self.assertEqual(watch.real_bases(self._entries()),
                         set(["22.03-LTS-SP4", "24.03-LTS-SP1"]))


class TestRefresh(WatchCase):
    """netbsd's RC trap, the other side: the confs already exist for the
    hook-reported version, but their URLs still pin the RC media. When
    the FINAL directory appears the watcher must move the URLs, not say
    'already has a conf, nothing to do'."""

    RC_URL = "https://x/pub/N/N-11.0_RC7/images/N-11.0_RC7-amd64.iso"
    FINAL_URL = "https://x/pub/N/N-11.0/images/N-11.0-amd64.iso"

    def _load(self):
        gendata = __import__("gendata")
        os_name, entries = gendata.scan_confs()
        notes = gendata.parse_notes()
        return os_name, entries, notes

    def test_decide_reports_refresh_for_rc_urls(self):
        self.add("demo-11.0.conf",
                 conf_text("demo", "11.0", url=self.RC_URL))
        os_name, entries, notes = self._load()
        self.assertEqual(watch.decide(os_name, entries, notes, "11.0"),
                         ("refresh", "11.0"))

    def test_decide_is_noop_once_urls_are_final(self):
        self.add("demo-11.0.conf",
                 conf_text("demo", "11.0", url=self.FINAL_URL))
        os_name, entries, notes = self._load()
        self.assertEqual(watch.decide(os_name, entries, notes, "11.0")[0],
                         "none")

    def test_plan_rewrites_only_url_lines(self):
        text = conf_text("demo", "11.0", url=self.RC_URL,
                         extra='VM_LOGIN_TAG="RC7 lookalike stays"')
        self.add("demo-11.0.conf", text)
        os_name, entries, notes = self._load()
        plan = watch.plan_refresh(os_name, entries, "11.0")
        self.assertEqual(len(plan), 1)
        item = plan[0]
        self.assertTrue(item["path"].endswith("demo-11.0.conf"))
        self.assertIn(self.FINAL_URL, item["content"])
        self.assertNotIn("11.0_RC7", item["content"].split("VM_LOGIN")[0])
        # a non-URL value keeps its RC-looking text
        self.assertIn('VM_LOGIN_TAG="RC7 lookalike stays"', item["content"])
        self.assertEqual(item["urls"], [self.FINAL_URL])

    def test_conf_without_rc_url_is_not_planned(self):
        self.add("demo-11.0.conf",
                 conf_text("demo", "11.0", url=self.FINAL_URL))
        self.add("demo-11.0-aarch64.conf",
                 conf_text("demo", "11.0", arch="aarch64", url=self.RC_URL))
        os_name, entries, notes = self._load()
        plan = watch.plan_refresh(os_name, entries, "11.0")
        self.assertEqual([os.path.basename(p["path"]) for p in plan],
                         ["demo-11.0-aarch64.conf"])

    def test_main_refresh_writes_and_is_head_gated(self):
        self.add("demo-11.0.conf",
                 conf_text("demo", "11.0", url=self.RC_URL))
        self.hook('print("11.0")')
        watch._TEST_OPENER = FakeOpener({self.FINAL_URL: 200})
        self.addCleanup(setattr, watch, "_TEST_OPENER", None)
        self.assertEqual(watch.main([]), 0)
        data = open("conf/demo-11.0.conf", encoding="utf-8").read()
        self.assertIn(self.FINAL_URL, data)
        self.assertNotIn("11.0_RC7", data)

    def test_main_refresh_aborts_on_dead_final_url(self):
        self.add("demo-11.0.conf",
                 conf_text("demo", "11.0", url=self.RC_URL))
        self.hook('print("11.0")')
        watch._TEST_OPENER = FakeOpener({})   # final URL 404s
        self.addCleanup(setattr, watch, "_TEST_OPENER", None)
        self.assertEqual(watch.main([]), 1)
        data = open("conf/demo-11.0.conf", encoding="utf-8").read()
        self.assertIn("11.0_RC7", data)      # untouched

    def test_check_mode_refresh_writes_nothing(self):
        self.add("demo-11.0.conf",
                 conf_text("demo", "11.0", url=self.RC_URL))
        self.hook('print("11.0")')
        watch._TEST_OPENER = FakeOpener({self.FINAL_URL: 200})
        self.addCleanup(setattr, watch, "_TEST_OPENER", None)
        self.assertEqual(watch.main(["--check"]), 0)
        self.assertIn("11.0_RC7",
                      open("conf/demo-11.0.conf", encoding="utf-8").read())


class TestSubstitute(unittest.TestCase):
    def test_compress_strips_punctuation(self):
        self.assertEqual(watch.compress("7.9"), "79")
        self.assertEqual(watch.compress("22.03-LTS-SP4"), "2203LTSSP4")

    def test_url_gets_both_dotted_and_compressed_form(self):
        old, new = "7.9", "8.0"
        v = "https://x/pub/OpenBSD/7.9/amd64/install79.iso"
        self.assertEqual(
            watch.substitute_value("VM_ISO_LINK", v, old, new),
            "https://x/pub/OpenBSD/8.0/amd64/install80.iso")

    def test_release_field_base_case(self):
        # true under the old overwrite-outright code too -- kept only as
        # the plain case; test_release_field_keeps_its_variant_suffix is
        # the one that pins the fix.
        self.assertEqual(
            watch.substitute_value("VM_RELEASE", "15.1", "15.1", "15.2"),
            "15.2")

    def test_release_field_keeps_its_variant_suffix(self):
        # The filename carries the suffix (freebsd-15.2-xfce.conf), so
        # VM_RELEASE must too -- gendata fatals on a mismatch and that
        # freezes the whole generation chain for the repo.
        self.assertEqual(
            watch.substitute_value("VM_RELEASE", "15.1-xfce",
                                   "15.1", "15.2"),
            "15.2-xfce")
        self.assertEqual(
            watch.substitute_value("VM_RELEASE", "r151058-build",
                                   "r151058", "r151060"),
            "r151060-build")


class TestPlanFiles(WatchCase):
    def test_replicates_every_variant(self):
        self.add("demo-15.1.conf", conf_text("demo", "15.1",
                 url="https://x/15.1/demo-15.1.qcow2"))
        self.add("demo-15.1-aarch64.conf", conf_text("demo", "15.1",
                 arch="aarch64", url="https://x/15.1/demo-15.1-arm.qcow2"))
        self.add("demo-15.1-xfce.conf",
                 conf_text("demo", "15.1-xfce",
                           extra='VM_EXTRA_SCRIPT="hooks/xfce.sh"'))
        os_name, entries = __import__("gendata").scan_confs()
        notes = __import__("gendata").parse_notes()
        plan = watch.plan_files(os_name, entries, notes, "15.1", "15.2")
        names = sorted(os.path.basename(p["path"]) for p in plan)
        self.assertEqual(names, ["demo-15.2-aarch64.conf",
                                 "demo-15.2-xfce.conf", "demo-15.2.conf"])
        base = [p for p in plan
                if p["path"].endswith("demo-15.2.conf")][0]
        self.assertIn("VM_RELEASE=15.2", base["content"])
        self.assertIn("https://x/15.2/demo-15.2.qcow2", base["content"])
        self.assertEqual(base["urls"], ["https://x/15.2/demo-15.2.qcow2"])

    def test_shared_opts_path_is_kept_when_substitute_missing(self):
        # freebsd's real shape: a 15.1 conf pointing at a 13.1 answer file
        write("conf/demo-13.1.opts.txt", "answers\n")
        self.add("demo-15.1.conf",
                 conf_text("demo", "15.1",
                           extra='VM_OPTS="conf/demo-13.1.opts.txt"'))
        os_name, entries = __import__("gendata").scan_confs()
        notes = __import__("gendata").parse_notes()
        plan = watch.plan_files(os_name, entries, notes, "15.1", "15.2")
        base = [p for p in plan if p["path"].endswith("demo-15.2.conf")][0]
        self.assertIn('VM_OPTS="conf/demo-13.1.opts.txt"', base["content"])

    def test_versioned_companion_file_is_copied(self):
        # openbsd's real shape: conf/<os>-<ver>.resp next to the conf
        write("conf/demo-7.9.resp", "set version 7.9\n")
        self.add("demo-7.9.conf",
                 conf_text("demo", "7.9",
                           extra='VM_OPTS="conf/demo-7.9.resp"'))
        os_name, entries = __import__("gendata").scan_confs()
        notes = __import__("gendata").parse_notes()
        plan = watch.plan_files(os_name, entries, notes, "7.9", "8.0")
        paths = sorted(os.path.basename(p["path"]) for p in plan)
        self.assertEqual(paths, ["demo-8.0.conf", "demo-8.0.resp"])
        conf = [p for p in plan if p["path"].endswith(".conf")][0]
        self.assertIn('VM_OPTS="conf/demo-8.0.resp"', conf["content"])
        resp = [p for p in plan if p["path"].endswith(".resp")][0]
        self.assertEqual(resp["content"], "set version 8.0\n")

    def test_unrelated_value_is_not_substituted(self):
        self.add("demo-7.9.conf",
                 conf_text("demo", "7.9",
                           url="https://x/7.9/demo79.img",
                           extra='VM_LOGIN_TAG="mirror79 build 17.90"'))
        os_name, entries = __import__("gendata").scan_confs()
        notes = __import__("gendata").parse_notes()
        plan = watch.plan_files(os_name, entries, notes, "7.9", "8.0")
        content = plan[0]["content"]
        # the URL field moves in both forms ...
        self.assertIn("https://x/8.0/demo80.img", content)
        # ... while an unrelated value keeps every digit it had
        self.assertIn('VM_LOGIN_TAG="mirror79 build 17.90"', content)
        self.assertIn("VM_RELEASE=8.0", content)

    def test_shelved_variant_is_not_replicated(self):
        self.add("demo-7.9.conf", conf_text("demo", "7.9"))
        self.add("demo-7.9-aarch64.conf",
                 conf_text("demo", "7.9", arch="aarch64"))
        write(__import__("gendata").NOTES_PATH,
              "<!-- shelved: 7.9-aarch64 -->\n")
        os_name, entries = __import__("gendata").scan_confs()
        notes = __import__("gendata").parse_notes()
        plan = watch.plan_files(os_name, entries, notes, "7.9", "8.0")
        names = sorted(os.path.basename(p["path"]) for p in plan)
        self.assertEqual(names, ["demo-8.0.conf"])

    def test_switched_off_variant_is_not_replicated(self):
        # a conf absent from all.release.conf is switched off; the
        # maintainer took that image out of the matrix, and replicating
        # it onto the new release would silently put it back
        self.add("demo-7.9.conf", conf_text("demo", "7.9"))
        self.add("demo-7.9-aarch64.conf",
                 conf_text("demo", "7.9", arch="aarch64"))
        write(__import__("gendata").MEMBERSHIP_PATH,
              "ALL_RELEASES='\"7.9\"'\n")
        os_name, entries = __import__("gendata").scan_confs()
        notes = __import__("gendata").parse_notes()
        membership = __import__("gendata").parse_membership()
        plan = watch.plan_files(os_name, entries, notes, "7.9", "8.0",
                                membership)
        names = sorted(os.path.basename(p["path"]) for p in plan)
        self.assertEqual(names, ["demo-8.0.conf"])

    def test_build_variant_is_replicated_and_its_url_moves(self):
        self.add("demo-r151058.conf", conf_text(
            "demo", "r151058", url="https://x/media/r151058/d-r151058.iso"))
        self.add("demo-r151058-build.conf", conf_text(
            "demo", "r151058-build",
            url="https://x/media/r151058/d-r151058.iso"))
        os_name, entries = __import__("gendata").scan_confs()
        notes = __import__("gendata").parse_notes()
        plan = watch.plan_files(os_name, entries, notes,
                                "r151058", "r151060")
        names = sorted(os.path.basename(p["path"]) for p in plan)
        self.assertEqual(names, ["demo-r151060-build.conf",
                                 "demo-r151060.conf"])
        build = [p for p in plan
                 if p["path"].endswith("demo-r151060-build.conf")][0]
        self.assertIn("VM_RELEASE=r151060-build", build["content"])
        # the whole point of C1: the URL must not stay on the old media
        self.assertEqual(build["urls"],
                         ["https://x/media/r151060/d-r151060.iso"])

    def test_chained_toolchain_variants_are_all_replicated(self):
        self.add("demo-11.4.conf", conf_text("demo", "11.4"))
        self.add("demo-11.4-gcc.conf", conf_text("demo", "11.4-gcc"))
        self.add("demo-11.4-gcc-14.conf", conf_text("demo", "11.4-gcc-14"))
        os_name, entries = __import__("gendata").scan_confs()
        notes = __import__("gendata").parse_notes()
        plan = watch.plan_files(os_name, entries, notes, "11.4", "11.5")
        names = sorted(os.path.basename(p["path"]) for p in plan)
        self.assertEqual(names, ["demo-11.5-gcc-14.conf",
                                 "demo-11.5-gcc.conf",
                                 "demo-11.5.conf"])


class TestCompanionSubstitution(WatchCase):
    def test_hash_and_timeouts_survive_only_the_path_moves(self):
        write("conf/demo-7.9.resp",
              "Password for root account = $2b$10$q79X.Lm79pQ\n"
              "Server directory = pub/Demo/7.9/amd64\n"
              "Installation | enter | 79\n")
        self.add("demo-7.9.conf",
                 conf_text("demo", "7.9",
                           extra='VM_OPTS="conf/demo-7.9.resp"'))
        os_name, entries = __import__("gendata").scan_confs()
        notes = __import__("gendata").parse_notes()
        plan = watch.plan_files(os_name, entries, notes, "7.9", "8.0")
        resp = [p for p in plan if p["path"].endswith(".resp")][0]
        self.assertIn("$2b$10$q79X.Lm79pQ", resp["content"])
        self.assertIn("pub/Demo/8.0/amd64", resp["content"])
        self.assertIn("Installation | enter | 79", resp["content"])

    def test_boundary_helper(self):
        self.assertEqual(watch.substitute_boundary("a/7.9/b", "7.9", "8.0"),
                         "a/8.0/b")
        self.assertEqual(watch.substitute_boundary("x7.9y", "7.9", "8.0"),
                         "x7.9y")
        self.assertEqual(watch.substitute_boundary("7.9", "7.9", "8.0"),
                         "8.0")

    def test_leftover_occurrence_is_warned(self):
        # A bare compressed-form token sitting between delimiters (not
        # glued to any alphanumeric) is a genuine miss -- substitute_
        # boundary never touches the compressed form in a companion file
        # at all, so this must still warn with file:line instead of
        # silently shipping a half-updated file.
        write("conf/demo-7.9.resp",
              "Server directory = pub/Demo/7.9/amd64\n"
              "Old build tag = 79\n")
        self.add("demo-7.9.conf",
                 conf_text("demo", "7.9",
                           extra='VM_OPTS="conf/demo-7.9.resp"'))
        os_name, entries = __import__("gendata").scan_confs()
        notes = __import__("gendata").parse_notes()
        seen = []
        real = watch.warn
        watch.warn = lambda m: seen.append(m)
        self.addCleanup(setattr, watch, "warn", real)
        plan = watch.plan_files(os_name, entries, notes, "7.9", "8.0")
        resp = [p for p in plan if p["path"].endswith(".resp")][0]
        self.assertIn("pub/Demo/8.0/amd64", resp["content"])
        self.assertIn("Old build tag = 79", resp["content"])
        self.assertTrue(any("demo-8.0.resp:2" in m for m in seen))

    def test_leftover_warning_skips_glued_digits(self):
        write("conf/demo-7.9.resp",
              "Password = $2b$10$q79X.Lm79pQ\n"
              "Server directory = pub/Demo/7.9/amd64\n")
        self.add("demo-7.9.conf",
                 conf_text("demo", "7.9",
                           extra='VM_OPTS="conf/demo-7.9.resp"'))
        seen = []
        real = watch.warn
        watch.warn = lambda m: seen.append(m)
        self.addCleanup(setattr, watch, "warn", real)
        os_name, entries = __import__("gendata").scan_confs()
        notes = __import__("gendata").parse_notes()
        watch.plan_files(os_name, entries, notes, "7.9", "8.0")
        self.assertEqual(seen, [])

    def test_suffix_style_new_version_does_not_warn_about_itself(self):
        write("conf/demo-24.03.resp",
              "Server directory = pub/Demo/24.03/amd64\n")
        self.add("demo-24.03.conf",
                 conf_text("demo", "24.03",
                           extra='VM_OPTS="conf/demo-24.03.resp"'))
        seen = []
        real = watch.warn
        watch.warn = lambda m: seen.append(m)
        self.addCleanup(setattr, watch, "warn", real)
        os_name, entries = __import__("gendata").scan_confs()
        notes = __import__("gendata").parse_notes()
        plan = watch.plan_files(os_name, entries, notes,
                                "24.03", "24.03-LTS-SP1")
        resp = [p for p in plan if p["path"].endswith(".resp")][0]
        self.assertIn("pub/Demo/24.03-LTS-SP1/amd64", resp["content"])
        self.assertEqual(seen, [])


class TestPlanIsGendataValid(WatchCase):
    """The plan must survive gendata, or generate.yml goes permanently red.

    gendata.scan_confs() requires the filename to be derivable from
    VM_RELEASE + VM_ARCH and calls fatal() (sys.exit) otherwise, and it
    runs as the FIRST step of generate.yml -- so a conf the watcher lands
    with a mismatched VM_RELEASE does not just fail one build, it stops
    build.py and every table from propagating into that repo at all.
    """

    def _write_plan(self, plan):
        for item in plan:
            with open(item["path"], "w", encoding="utf-8",
                      newline="\n") as f:
                f.write(item["content"])

    def test_desktop_variant_conf_is_accepted_by_gendata(self):
        self.add("demo-15.1.conf", conf_text("demo", "15.1"))
        self.add("demo-15.1-xfce.conf",
                 conf_text("demo", "15.1-xfce",
                           extra='VM_EXTRA_SCRIPT="hooks/xfce.sh"'))
        gendata = __import__("gendata")
        os_name, entries = gendata.scan_confs()
        notes = gendata.parse_notes()
        self._write_plan(watch.plan_files(os_name, entries, notes,
                                          "15.1", "15.2"))
        _, after = gendata.scan_confs()          # must not sys.exit
        self.assertIn("15.2-xfce", [e["tag"] for e in after])

    def test_build_variant_conf_is_accepted_by_gendata(self):
        self.add("demo-r151058.conf", conf_text("demo", "r151058"))
        self.add("demo-r151058-build.conf",
                 conf_text("demo", "r151058-build"))
        gendata = __import__("gendata")
        os_name, entries = gendata.scan_confs()
        notes = gendata.parse_notes()
        self._write_plan(watch.plan_files(os_name, entries, notes,
                                          "r151058", "r151060"))
        _, after = gendata.scan_confs()
        tags = [e["tag"] for e in after]
        self.assertIn("r151060", tags)
        self.assertIn("r151060-build", tags)


class TestFilenameGuard(unittest.TestCase):
    """A last-resort structural check, so a future substitution slip
    fails the watcher run instead of the repo's generation chain."""

    def test_matching_filename_passes(self):
        plan = [{"path": os.path.join("conf", "demo-15.2-xfce.conf"),
                 "content": "VM_RELEASE=15.2-xfce\n"}]
        self.assertEqual(watch.check_filenames("demo", plan), [])

    def test_stripped_variant_suffix_is_caught(self):
        plan = [{"path": os.path.join("conf", "demo-15.2-xfce.conf"),
                 "content": "VM_RELEASE=15.2\n"}]
        bad = watch.check_filenames("demo", plan)
        self.assertEqual(len(bad), 1)
        self.assertIn("demo-15.2-xfce.conf", bad[0])
        self.assertIn("demo-15.2.conf", bad[0])

    def test_arch_suffix_is_understood(self):
        plan = [{"path": os.path.join("conf", "demo-15.2-aarch64.conf"),
                 "content": 'VM_RELEASE=15.2\nVM_ARCH="aarch64"\n'}]
        self.assertEqual(watch.check_filenames("demo", plan), [])

    def test_companion_files_are_not_checked(self):
        plan = [{"path": os.path.join("conf", "demo-8.0.resp"),
                 "content": "anything\n"}]
        self.assertEqual(watch.check_filenames("demo", plan), [])


class TestUrlMovementGuard(unittest.TestCase):
    """The one thing a HEAD check cannot see: a URL that never moved."""

    def _conf(self, pairs):
        return [{"path": os.path.join("conf", "demo-8.0.conf"),
                 "url_pairs": pairs}]

    def test_moved_url_passes(self):
        self.assertEqual(watch.check_urls_moved(self._conf(
            [("VM_ISO_LINK", "https://x/7.9/a.iso",
              "https://x/8.0/a.iso")])), [])

    def test_unchanged_url_is_caught(self):
        bad = watch.check_urls_moved(self._conf(
            [("VM_ISO_LINK", "https://x/pinned.iso",
              "https://x/pinned.iso")]))
        self.assertEqual(len(bad), 1)
        self.assertIn("did not move", bad[0])
        self.assertIn("url-template", bad[0])

    def test_conf_without_any_url_is_caught(self):
        bad = watch.check_urls_moved(self._conf([]))
        self.assertEqual(len(bad), 1)
        self.assertIn("no VM_ISO_LINK", bad[0])

    def test_companion_file_is_exempt(self):
        plan = [{"path": os.path.join("conf", "demo-8.0.resp"),
                 "url_pairs": []}]
        self.assertEqual(watch.check_urls_moved(plan), [])


class FakeOpener(object):
    def __init__(self, codes):
        self.codes = codes
        self.seen = []

    def __call__(self, url):
        self.seen.append(url)
        code = self.codes.get(url, 404)
        if code >= 400:
            raise watch.UrlError("HTTP %d" % code)
        return code


class FlakyOpener(object):
    """Fails with a transport error `fails` times, then succeeds."""

    def __init__(self, fails, error="timed out"):
        self.left = fails
        self.error = error
        self.calls = 0

    def __call__(self, url):
        self.calls += 1
        if self.left > 0:
            self.left -= 1
            raise watch.UrlError(self.error)
        return 200


class TestUrlRetry(unittest.TestCase):
    """A blip must not file a bug report about a healthy release."""

    def setUp(self):
        self.slept = []

    def test_transient_failure_is_retried(self):
        op = FlakyOpener(2)
        ok, detail = watch.check_url("https://x/a", opener=op,
                                     sleep=self.slept.append)
        self.assertEqual((ok, detail), (True, ""))
        self.assertEqual(op.calls, 3)
        self.assertEqual(len(self.slept), 2)

    def test_http_status_is_not_retried(self):
        op = FakeOpener({})            # every URL 404s
        ok, detail = watch.check_url("https://x/missing", opener=op,
                                     sleep=self.slept.append)
        self.assertEqual((ok, detail), (False, "HTTP 404"))
        self.assertEqual(op.seen, ["https://x/missing"])
        self.assertEqual(self.slept, [])

    def test_persistent_transport_failure_still_fails(self):
        op = FlakyOpener(99)
        ok, detail = watch.check_url("https://x/a", opener=op,
                                     sleep=self.slept.append)
        self.assertFalse(ok)
        self.assertEqual(detail, "timed out")
        self.assertEqual(op.calls, 3)


class TestCompanionGuard(unittest.TestCase):
    def _item(self, name, key, leftovers=()):
        return {"path": os.path.join("conf", name), "source": "conf/old",
                "companion_key": key, "leftovers": list(leftovers)}

    def test_answer_file_is_allowed(self):
        self.assertEqual(
            watch.check_companions([self._item("d-8.0.resp", "VM_OPTS")]), [])

    def test_install_script_is_refused(self):
        bad = watch.check_companions(
            [self._item("d-r1beta6-pinned.sh", "VM_INSTALL_SCRIPT")])
        self.assertEqual(len(bad), 1)
        self.assertIn("VM_INSTALL_SCRIPT", bad[0])
        self.assertIn("by hand", bad[0])

    def test_surviving_version_token_is_refused(self):
        bad = watch.check_companions(
            [self._item("d-8.0.resp", "VM_OPTS", leftovers=[2, 7])])
        self.assertEqual(len(bad), 1)
        self.assertIn("2, 7", bad[0])

    def test_conf_items_are_ignored(self):
        self.assertEqual(watch.check_companions(
            [{"path": "conf/d-8.0.conf", "companion_key": None,
              "leftovers": []}]), [])


class TestVerify(unittest.TestCase):
    def test_all_ok(self):
        plan = [{"path": "conf/a.conf", "urls": ["https://x/a"]}]
        op = FakeOpener({"https://x/a": 200})
        self.assertEqual(watch.verify(plan, opener=op), [])

    def test_failure_is_reported_with_the_url(self):
        plan = [{"path": "conf/a.conf", "urls": ["https://x/missing"]}]
        op = FakeOpener({})
        bad = watch.verify(plan, opener=op)
        self.assertEqual(len(bad), 1)
        self.assertIn("https://x/missing", bad[0])
        self.assertIn("conf/a.conf", bad[0])

    def test_conf_without_url_is_not_a_failure(self):
        plan = [{"path": "conf/a.conf", "urls": []}]
        self.assertEqual(watch.verify(plan, opener=FakeOpener({})), [])


class TestUrlTemplate(WatchCase):
    def test_template_beats_substitution(self):
        self.add("demo-7.9.conf", conf_text(
            "demo", "7.9", url="https://x/wrong/7.9.img"))
        write(__import__("gendata").NOTES_PATH,
              "<!-- url-template: VM_VHD_LINK = "
              "https://x/{V}/demo{VC}.img -->\n")
        os_name, entries = __import__("gendata").scan_confs()
        notes = __import__("gendata").parse_notes()
        plan = watch.plan_files(os_name, entries, notes, "7.9", "8.0")
        self.assertIn("https://x/8.0/demo80.img", plan[0]["content"])


class TestAllowSuggestions(unittest.TestCase):
    def test_suffix_match_produces_a_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            allow = os.path.join(tmp, "coverage.allow")
            with open(allow, "w") as f:
                f.write("# untested build variants\n")
                f.write("demo r1-build demo.yml\n")
            plan = [{"path": "conf/demo-r2-build.conf", "urls": []},
                    {"path": "conf/demo-r2.conf", "urls": []}]
            lines = watch.suggest_allow_lines("demo", plan, allow)
        self.assertEqual(lines, ["demo r2-build demo.yml"])

    def test_no_matching_suffix_means_no_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            allow = os.path.join(tmp, "coverage.allow")
            with open(allow, "w") as f:
                f.write("demo * testwindows.yml\n")
            plan = [{"path": "conf/demo-r2.conf", "urls": []}]
            self.assertEqual(watch.suggest_allow_lines("demo", plan, allow),
                             [])


class TestMain(WatchCase):
    def test_check_mode_writes_nothing(self):
        self.add("demo-15.1.conf", conf_text(
            "demo", "15.1", url="https://x/15.1.img"))
        self.hook('print("15.2")')
        watch._TEST_OPENER = FakeOpener({"https://x/15.2.img": 200})
        self.addCleanup(setattr, watch, "_TEST_OPENER", None)
        self.assertEqual(watch.main(["--check"]), 0)
        self.assertFalse(os.path.exists("conf/demo-15.2.conf"))

    def test_writes_when_not_checking(self):
        self.add("demo-15.1.conf", conf_text(
            "demo", "15.1", url="https://x/15.1.img"))
        self.hook('print("15.2")')
        watch._TEST_OPENER = FakeOpener({"https://x/15.2.img": 200})
        self.addCleanup(setattr, watch, "_TEST_OPENER", None)
        self.assertEqual(watch.main([]), 0)
        self.assertTrue(os.path.exists("conf/demo-15.2.conf"))

    def test_landed_tags_are_switched_on_in_membership(self):
        # without the append, the watcher's confs exist but never enter
        # the build matrix -- all.release.conf is the hand-owned switch
        self.add("demo-15.1.conf", conf_text(
            "demo", "15.1", url="https://x/15.1.img"))
        self.add("demo-15.1-aarch64.conf", conf_text(
            "demo", "15.1", arch="aarch64", url="https://x/15.1-arm.img"))
        write("conf/all.release.conf",
              "ALL_RELEASES='\"15.1\", \"15.1-aarch64\"'\n")
        self.hook('print("15.2")')
        watch._TEST_OPENER = FakeOpener({"https://x/15.2.img": 200,
                                         "https://x/15.2-arm.img": 200})
        self.addCleanup(setattr, watch, "_TEST_OPENER", None)
        self.assertEqual(watch.main([]), 0)
        text = open("conf/all.release.conf", encoding="utf-8").read()
        self.assertEqual(
            text,
            "ALL_RELEASES='\"15.1\", \"15.1-aarch64\", "
            "\"15.2\", \"15.2-aarch64\"'\n")

    def test_replace_mode_swaps_the_membership(self):
        # 9front's shape: publishing the new image DELETES the old one,
        # so plan9's membership file declares ALL_RELEASES_UPDATE=replace
        # and the superseded release leaves the matrix in the same commit
        # -- the build never sees an unbuildable release.
        self.add("demo-11554.conf", conf_text(
            "demo", "11554", url="https://x/iso/demo-11554.qcow2.gz"))
        write("conf/all.release.conf",
              "ALL_RELEASES='\"11554\"'\n"
              "ALL_RELEASES_UPDATE=replace\n")
        self.hook('print("11952")')
        watch._TEST_OPENER = FakeOpener(
            {"https://x/iso/demo-11952.qcow2.gz": 200})
        self.addCleanup(setattr, watch, "_TEST_OPENER", None)
        self.assertEqual(watch.main([]), 0)
        text = open("conf/all.release.conf", encoding="utf-8").read()
        self.assertEqual(text,
                         "ALL_RELEASES='\"11952\"'\n"
                         "ALL_RELEASES_UPDATE=replace\n")

    def test_default_mode_appends_and_keeps_the_old_release(self):
        # freebsd's shape: old media stays published; no mode line means
        # append, the old release keeps building
        self.add("demo-15.1.conf", conf_text(
            "demo", "15.1", url="https://x/15.1.img"))
        write("conf/all.release.conf", "ALL_RELEASES='\"15.1\"'\n")
        self.hook('print("15.2")')
        watch._TEST_OPENER = FakeOpener({"https://x/15.2.img": 200})
        self.addCleanup(setattr, watch, "_TEST_OPENER", None)
        self.assertEqual(watch.main([]), 0)
        text = open("conf/all.release.conf", encoding="utf-8").read()
        self.assertEqual(text, "ALL_RELEASES='\"15.1\", \"15.2\"'\n")

    def test_unknown_update_mode_is_fatal(self):
        write("conf/all.release.conf",
              "ALL_RELEASES='\"1\"'\nALL_RELEASES_UPDATE=rolling\n")
        with self.assertRaises(SystemExit):
            watch.membership_update_mode()

    def test_replace_mode_check_prints_and_writes_nothing(self):
        self.add("demo-11554.conf", conf_text(
            "demo", "11554", url="https://x/iso/demo-11554.qcow2.gz"))
        write("conf/all.release.conf",
              "ALL_RELEASES='\"11554\"'\n"
              "ALL_RELEASES_UPDATE=replace\n")
        self.hook('print("11952")')
        watch._TEST_OPENER = FakeOpener(
            {"https://x/iso/demo-11952.qcow2.gz": 200})
        self.addCleanup(setattr, watch, "_TEST_OPENER", None)
        self.assertEqual(watch.main(["--check"]), 0)
        text = open("conf/all.release.conf", encoding="utf-8").read()
        self.assertIn('"11554"', text)
        self.assertNotIn('"11952"', text)

    def test_landed_out_written_on_landing(self):
        # the workflow turns this file into the "upstream release landed"
        # notification issue -- without it a landing is silent and the
        # maintainer never learns a tag cut is wanted (midnightbsd 4.0.7
        # landed on 2026-08-01 and nobody noticed)
        self.add("demo-15.1.conf", conf_text(
            "demo", "15.1", url="https://x/15.1.img"))
        write("conf/all.release.conf", "ALL_RELEASES='\"15.1\"'\n")
        self.hook('print("15.2")')
        watch._TEST_OPENER = FakeOpener({"https://x/15.2.img": 200})
        self.addCleanup(setattr, watch, "_TEST_OPENER", None)
        self.assertEqual(watch.main(["--landed-out", "landed.txt"]), 0)
        self.assertEqual(open("landed.txt", encoding="utf-8").read(),
                         "new 15.2\n")

    def test_landed_out_written_on_refresh(self):
        self.add("demo-11.0.conf", conf_text(
            "demo", "11.0",
            url="https://x/N-11.0_RC7/N-11.0_RC7-amd64.iso"))
        self.hook('print("11.0")')
        watch._TEST_OPENER = FakeOpener(
            {"https://x/N-11.0/N-11.0-amd64.iso": 200})
        self.addCleanup(setattr, watch, "_TEST_OPENER", None)
        self.assertEqual(watch.main(["--landed-out", "landed.txt"]), 0)
        self.assertEqual(open("landed.txt", encoding="utf-8").read(),
                         "refresh 11.0\n")

    def test_landed_out_absent_on_noop_and_check(self):
        self.add("demo-15.1.conf", conf_text(
            "demo", "15.1", url="https://x/15.1.img"))
        self.hook('print("15.1")')
        self.assertEqual(watch.main(["--landed-out", "landed.txt"]), 0)
        self.assertFalse(os.path.exists("landed.txt"))
        self.hook('print("15.2")')
        watch._TEST_OPENER = FakeOpener({"https://x/15.2.img": 200})
        self.addCleanup(setattr, watch, "_TEST_OPENER", None)
        self.assertEqual(watch.main(["--check", "--landed-out",
                                     "landed.txt"]), 0)
        self.assertFalse(os.path.exists("landed.txt"))

    def test_check_mode_does_not_touch_membership(self):
        self.add("demo-15.1.conf", conf_text(
            "demo", "15.1", url="https://x/15.1.img"))
        write("conf/all.release.conf", "ALL_RELEASES='\"15.1\"'\n")
        self.hook('print("15.2")')
        watch._TEST_OPENER = FakeOpener({"https://x/15.2.img": 200})
        self.addCleanup(setattr, watch, "_TEST_OPENER", None)
        self.assertEqual(watch.main(["--check"]), 0)
        self.assertEqual(open("conf/all.release.conf").read(),
                         "ALL_RELEASES='\"15.1\"'\n")

    def test_membership_append_preserves_crlf(self):
        with open("conf/all.release.conf", "wb") as f:
            f.write(b"ALL_RELEASES='\"15.1\"'\r\n")
        added = watch.append_membership(["15.2"])
        self.assertEqual(added, ["15.2"])
        data = open("conf/all.release.conf", "rb").read()
        self.assertEqual(data, b"ALL_RELEASES='\"15.1\", \"15.2\"'\r\n")

    def test_bad_url_aborts_and_writes_nothing(self):
        self.add("demo-15.1.conf", conf_text(
            "demo", "15.1", url="https://x/15.1.img"))
        self.hook('print("15.2")')
        watch._TEST_OPENER = FakeOpener({})   # every URL 404s
        self.addCleanup(setattr, watch, "_TEST_OPENER", None)
        self.assertEqual(watch.main([]), 1)
        self.assertFalse(os.path.exists("conf/demo-15.2.conf"))

    def test_broken_hook_is_an_error(self):
        self.add("demo-15.1.conf", conf_text("demo", "15.1"))
        self.hook('import sys; sys.exit(7)')
        self.assertEqual(watch.main([]), 1)

    def test_no_hook_is_a_clean_noop(self):
        self.add("demo-15.1.conf", conf_text("demo", "15.1"))
        self.assertEqual(watch.main([]), 0)

    def test_allow_lines_are_written_out_for_the_commit_message(self):
        self.add("demo-r1.conf", conf_text(
            "demo", "r1", url="https://x/r1.img"))
        self.add("demo-r1-build.conf", conf_text(
            "demo", "r1-build", url="https://x/r1.img"))
        write("allow.txt", "demo r1-build demo.yml\n")
        self.hook('print("r2")')
        watch._TEST_OPENER = FakeOpener({"https://x/r2.img": 200})
        self.addCleanup(setattr, watch, "_TEST_OPENER", None)
        rc = watch.main(["--allow-file", "allow.txt",
                         "--allow-out", "out.txt"])
        self.assertEqual(rc, 0)
        with open("out.txt", encoding="utf-8") as f:
            self.assertEqual(f.read(), "demo r2-build demo.yml\n")

    def test_check_mode_writes_no_allow_file(self):
        self.add("demo-r1.conf", conf_text(
            "demo", "r1", url="https://x/r1.img"))
        self.add("demo-r1-build.conf", conf_text(
            "demo", "r1-build", url="https://x/r1.img"))
        write("allow.txt", "demo r1-build demo.yml\n")
        self.hook('print("r2")')
        watch._TEST_OPENER = FakeOpener({"https://x/r2.img": 200})
        self.addCleanup(setattr, watch, "_TEST_OPENER", None)
        watch.main(["--check", "--allow-file", "allow.txt",
                    "--allow-out", "out.txt"])
        self.assertFalse(os.path.exists("out.txt"))

    def test_refuses_a_release_scoped_install_script(self):
        # haiku's real shape: a VM_INSTALL_SCRIPT in conf/ pinning
        # release-era package FILENAMES that the version substitution
        # cannot possibly get right
        write("conf/demo-r1beta5-pinned.sh",
              "# pinned for r1beta5\n"
              'PINNED="glib2-2.78.0-2-x86_64.hpkg"\n')
        self.add("demo-r1beta5.conf", conf_text(
            "demo", "r1beta5", url="https://x/r1beta5.iso",
            extra='VM_INSTALL_SCRIPT="conf/demo-r1beta5-pinned.sh"'))
        self.hook('print("r1beta6")')
        watch._TEST_OPENER = FakeOpener({"https://x/r1beta6.iso": 200})
        self.addCleanup(setattr, watch, "_TEST_OPENER", None)
        self.assertEqual(watch.main([]), 1)
        self.assertFalse(os.path.exists("conf/demo-r1beta6.conf"))
        self.assertFalse(os.path.exists("conf/demo-r1beta6-pinned.sh"))

    def test_allows_an_answer_file_companion(self):
        write("conf/demo-7.9.resp", "Server directory = pub/D/7.9/amd64\n")
        self.add("demo-7.9.conf", conf_text(
            "demo", "7.9", url="https://x/7.9/install79.iso",
            extra='VM_OPTS="conf/demo-7.9.resp"'))
        self.hook('print("8.0")')
        watch._TEST_OPENER = FakeOpener(
            {"https://x/8.0/install80.iso": 200})
        self.addCleanup(setattr, watch, "_TEST_OPENER", None)
        self.assertEqual(watch.main([]), 0)
        self.assertTrue(os.path.exists("conf/demo-8.0.resp"))

    def test_refuses_a_conf_whose_url_never_moved(self):
        # a pinned release-asset URL with no version in it: the HEAD gate
        # would return 200 on the OLD media and land a mislabelled image
        self.add("demo-15.1.conf", conf_text(
            "demo", "15.1", url="https://x/pinned/v1/demo.img"))
        self.hook('print("15.2")')
        watch._TEST_OPENER = FakeOpener({"https://x/pinned/v1/demo.img": 200})
        self.addCleanup(setattr, watch, "_TEST_OPENER", None)
        self.assertEqual(watch.main([]), 1)
        self.assertFalse(os.path.exists("conf/demo-15.2.conf"))

    def test_refuses_a_conf_gendata_would_reject(self):
        self.add("demo-15.1.conf", conf_text(
            "demo", "15.1", url="https://x/15.1.img"))
        self.hook('print("15.2")')
        watch._TEST_OPENER = FakeOpener({"https://x/15.2.img": 200})
        self.addCleanup(setattr, watch, "_TEST_OPENER", None)
        real = watch.check_filenames
        watch.check_filenames = lambda o, p: ["demo-15.2.conf: forced"]
        self.addCleanup(setattr, watch, "check_filenames", real)
        self.assertEqual(watch.main([]), 1)
        self.assertFalse(os.path.exists("conf/demo-15.2.conf"))

    def test_refuses_to_write_outside_conf(self):
        os.makedirs("hooks")
        write("hooks/demo-15.1.answers", "version 15.1\n")
        self.add("demo-15.1.conf",
                 conf_text("demo", "15.1", url="https://x/15.1.img",
                           extra='VM_OPTS="hooks/demo-15.1.answers"'))
        self.hook('print("15.2")')
        watch._TEST_OPENER = FakeOpener({"https://x/15.2.img": 200})
        self.addCleanup(setattr, watch, "_TEST_OPENER", None)
        self.assertEqual(watch.main([]), 1)
        self.assertFalse(os.path.exists("conf/demo-15.2.conf"))
        self.assertFalse(os.path.exists("hooks/demo-15.2.answers"))


if __name__ == "__main__":
    unittest.main()
