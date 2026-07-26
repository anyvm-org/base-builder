import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gendata


def write(path, content):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


def conf_text(os_name, release, arch="", sync="rsync,scp", extra="",
              shutdown=""):
    lines = ['VM_OS_NAME="%s"' % os_name, "VM_RELEASE=%s" % release]
    if arch:
        lines.append("VM_ARCH=%s" % arch)
    if sync:
        lines.append('VM_SYNC_METHODS="%s"' % sync)
    if extra:
        lines.append('VM_EXTRA_SCRIPT="%s"' % extra)
    if shutdown:
        lines.append('VM_SHUTDOWN_CMD="%s"' % shutdown)
    return "\n".join(lines) + "\n"


class GendataCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        old = os.getcwd()
        self.addCleanup(os.chdir, old)
        os.chdir(tmp.name)
        os.makedirs("conf")

    def add(self, name, content):
        write(os.path.join("conf", name), content)


class TestScanConfs(GendataCase):
    def test_basic_scan(self):
        self.add("demo-14.4.conf", conf_text("demo", "14.4"))
        self.add("demo-14.4-aarch64.conf",
                 conf_text("demo", "14.4", arch="aarch64", sync="nfs,scp"))
        os_name, entries = gendata.scan_confs()
        self.assertEqual(os_name, "demo")
        by_tag = dict((e["tag"], e) for e in entries)
        self.assertEqual(set(by_tag), {"14.4", "14.4-aarch64"})
        self.assertEqual(by_tag["14.4"]["arch"], "x86_64")
        self.assertEqual(by_tag["14.4"]["sync"], "rsync,scp")
        self.assertFalse(by_tag["14.4"]["desktop"])
        self.assertEqual(by_tag["14.4-aarch64"]["arch"], "aarch64")
        self.assertEqual(by_tag["14.4-aarch64"]["sync"], "nfs,scp")

    def test_desktop_flag_from_extra_script(self):
        self.add("demo-15.1.conf", conf_text("demo", "15.1"))
        self.add("demo-15.1-xfce.conf",
                 conf_text("demo", "15.1-xfce", extra="hooks/xfce.sh"))
        os_name, entries = gendata.scan_confs()
        by_tag = dict((e["tag"], e) for e in entries)
        self.assertTrue(by_tag["15.1-xfce"]["desktop"])
        self.assertFalse(by_tag["15.1"]["desktop"])

    def test_hyphenated_release(self):
        self.add("demo-22.03-LTS-SP4-aarch64.conf",
                 conf_text("demo", "22.03-LTS-SP4", arch="aarch64"))
        os_name, entries = gendata.scan_confs()
        self.assertEqual(entries[0]["release"], "22.03-LTS-SP4")
        self.assertEqual(entries[0]["tag"], "22.03-LTS-SP4-aarch64")

    def test_os_name_mismatch_fatal(self):
        self.add("demo-1.0.conf", conf_text("demo", "1.0"))
        self.add("demo-2.0.conf", conf_text("other", "2.0"))
        with self.assertRaises(SystemExit):
            gendata.scan_confs()

    def test_filename_mismatch_fatal(self):
        self.add("demo-1.0.conf", conf_text("demo", "9.9"))
        with self.assertRaises(SystemExit):
            gendata.scan_confs()

    def test_all_release_conf_ignored(self):
        self.add("demo-1.0.conf", conf_text("demo", "1.0"))
        self.add("all.release.conf", "ALL_RELEASES='\"1.0\"'\n")
        os_name, entries = gendata.scan_confs()
        self.assertEqual(len(entries), 1)

    def test_ambient_vm_env_does_not_leak(self):
        self.add("demo-1.0.conf", conf_text("demo", "1.0"))
        os.environ["VM_EXTRA_SCRIPT"] = "hooks/leak.sh"
        self.addCleanup(os.environ.pop, "VM_EXTRA_SCRIPT", None)
        os_name, entries = gendata.scan_confs()
        self.assertFalse(entries[0]["desktop"])

    def test_shutdown_cmd_captured(self):
        self.add("demo-1.0.conf",
                 conf_text("demo", "1.0", shutdown="shutdown -p now"))
        os_name, entries = gendata.scan_confs()
        self.assertEqual(entries[0]["shutdown"], "shutdown -p now")

    def test_shutdown_cmd_empty_by_default(self):
        self.add("demo-1.0.conf", conf_text("demo", "1.0"))
        os_name, entries = gendata.scan_confs()
        self.assertEqual(entries[0]["shutdown"], "")


class TestOrdering(GendataCase):
    def test_all_release_ordering(self):
        self.add("demo-9.0.conf", conf_text("demo", "9.0"))
        self.add("demo-10.0.conf", conf_text("demo", "10.0"))
        self.add("demo-10.0-aarch64.conf",
                 conf_text("demo", "10.0", arch="aarch64"))
        self.add("demo-10.0-sparc64.conf",
                 conf_text("demo", "10.0", arch="sparc64"))
        self.add("demo-10.0-riscv64.conf",
                 conf_text("demo", "10.0", arch="riscv64"))
        self.add("demo-10.0-xfce.conf",
                 conf_text("demo", "10.0-xfce", extra="hooks/xfce.sh"))
        self.add("demo-10.0-gnome.conf",
                 conf_text("demo", "10.0-gnome", extra="hooks/gnome.sh"))
        entries = gendata.scan_confs()[1]
        out = gendata.render_all_release(entries)
        self.assertEqual(
            out,
            "ALL_RELEASES='\"9.0\", \"10.0\", \"10.0-aarch64\", "
            "\"10.0-riscv64\", \"10.0-sparc64\", \"10.0-gnome\", "
            "\"10.0-xfce\"'\n")

    def test_natural_sort_hyphenated(self):
        self.assertLess(gendata.natural_key("22.03-LTS-SP4"),
                        gendata.natural_key("24.03-LTS-SP4"))
        self.assertLess(gendata.natural_key("9.4"),
                        gendata.natural_key("10.0"))

    def test_releases_json(self):
        self.add("demo-1.0.conf", conf_text("demo", "1.0", sync="nfs,scp"))
        self.add("demo-1.0-xfce.conf",
                 conf_text("demo", "1.0-xfce", extra="hooks/xfce.sh"))
        os_name, entries = gendata.scan_confs()
        notes = gendata.parse_notes()
        data = json.loads(
            gendata.render_releases_json(os_name, entries, notes))
        self.assertEqual(data["os"], "demo")
        self.assertEqual(data["releases"][0], {
            "tag": "1.0", "release": "1.0", "arch": "x86_64",
            "sync": "nfs,scp", "shutdown": "", "desktop": False,
            "build": True})
        self.assertTrue(data["releases"][1]["desktop"])

    def test_releases_json_shutdown_field(self):
        self.add("demo-1.0.conf",
                 conf_text("demo", "1.0", sync="nfs,scp",
                           shutdown="shutdown -p now"))
        os_name, entries = gendata.scan_confs()
        notes = gendata.parse_notes()
        text = gendata.render_releases_json(os_name, entries, notes)
        data = json.loads(text)
        self.assertEqual(data["releases"][0], {
            "tag": "1.0", "release": "1.0", "arch": "x86_64",
            "sync": "nfs,scp", "shutdown": "shutdown -p now",
            "desktop": False, "build": True})
        # Key order matters for readable diffs: "shutdown" must sit
        # between "sync" and "desktop" in the raw JSON text.
        sync_idx = text.index('"sync"')
        shutdown_idx = text.index('"shutdown"')
        desktop_idx = text.index('"desktop"')
        self.assertLess(sync_idx, shutdown_idx)
        self.assertLess(shutdown_idx, desktop_idx)


NOTES_SAMPLE = (
    "<!-- absent: 13.4-riscv64 rv-stub -->\n"
    "<!-- arch-label: aarch64 = aarch64(arm64) -->\n"
    "[^rv-stub]: broken upstream image.\n")


class TestNotes(GendataCase):
    def test_parse_notes(self):
        write(gendata.NOTES_PATH, NOTES_SAMPLE)
        notes = gendata.parse_notes()
        self.assertEqual(notes["absent"], {"13.4-riscv64": "rv-stub"})
        self.assertEqual(notes["arch_labels"], {"aarch64": "aarch64(arm64)"})
        self.assertEqual(notes["raw"], NOTES_SAMPLE)

    def test_undefined_footnote_fatal(self):
        write(gendata.NOTES_PATH, "<!-- absent: 1.0-x86_64 nope -->\n")
        with self.assertRaises(SystemExit):
            gendata.parse_notes()

    def test_missing_notes_file_ok(self):
        notes = gendata.parse_notes()
        self.assertEqual(notes["absent"], {})
        self.assertEqual(notes["raw"], "")

    def test_extra_columns(self):
        write(gendata.NOTES_PATH,
              "<!-- release-label: Release (BlissOS) -->\n"
              "<!-- extra-column: Android -->\n"
              "<!-- extra-value: 16 13 -->\n")
        notes = gendata.parse_notes()
        self.assertEqual(notes["release_label"], "Release (BlissOS)")
        self.assertEqual(notes["extra_columns"], ["Android"])
        self.assertEqual(notes["extra_values"], {("Android", "16"): "13"})


class TestRenderTable(GendataCase):
    def test_table_grid(self):
        self.add("demo-14.4.conf", conf_text("demo", "14.4"))
        self.add("demo-14.4-aarch64.conf",
                 conf_text("demo", "14.4", arch="aarch64", sync="nfs,scp"))
        self.add("demo-15.0.conf", conf_text("demo", "15.0"))
        entries = gendata.scan_confs()[1]
        notes = gendata.parse_notes()
        out = gendata.render_table(entries, notes)
        self.assertEqual(
            out,
            "\n\n"
            "| Release | x86_64 | aarch64 |\n"
            "|---------|---------|---------|\n"
            "| 15.0 | \u2705 (rsync,scp) | \u2014 |\n"
            "| 14.4 | \u2705 (rsync,scp) | \u2705 (nfs,scp) |\n"
            "\n")

    def test_absent_footnote_and_notes_appended(self):
        self.add("demo-13.4.conf", conf_text("demo", "13.4"))
        self.add("demo-13.5.conf", conf_text("demo", "13.5"))
        self.add("demo-13.5-riscv64.conf",
                 conf_text("demo", "13.5", arch="riscv64"))
        write(gendata.NOTES_PATH, NOTES_SAMPLE)
        entries = gendata.scan_confs()[1]
        notes = gendata.parse_notes()
        out = gendata.render_table(entries, notes)
        self.assertIn("| 13.4 | \u2705 (rsync,scp) | \u2014[^rv-stub] |", out)
        self.assertIn("| aarch64(arm64) |".replace("aarch64(arm64)", "riscv64"),
                      out)
        self.assertTrue(out.endswith(NOTES_SAMPLE))

    def test_desktop_confs_excluded_from_main_table(self):
        self.add("demo-15.0.conf", conf_text("demo", "15.0"))
        self.add("demo-15.0-xfce.conf",
                 conf_text("demo", "15.0-xfce", extra="hooks/xfce.sh"))
        entries = gendata.scan_confs()[1]
        out = gendata.render_table(entries, gendata.parse_notes())
        self.assertNotIn("15.0-xfce", out)

    def test_force_desktop_override(self):
        self.add("demo-16.conf", conf_text("demo", "16"))
        write(gendata.NOTES_PATH, "<!-- desktop-table: 16 -->\n")
        os_name, entries = gendata.scan_confs()
        notes = gendata.parse_notes()
        gendata.apply_overrides(entries, notes)
        self.assertTrue(entries[0]["desktop"])


class TestRenderDesktop(GendataCase):
    def test_none_without_desktop_confs(self):
        self.add("demo-1.0.conf", conf_text("demo", "1.0"))
        os_name, entries = gendata.scan_confs()
        self.assertIsNone(
            gendata.render_desktop(os_name, entries, gendata.parse_notes()))

    def test_desktop_table(self):
        self.add("demo-15.1.conf", conf_text("demo", "15.1"))
        self.add("demo-15.1-xfce.conf",
                 conf_text("demo", "15.1-xfce", extra="hooks/xfce.sh"))
        write(gendata.NOTES_PATH,
              "<!-- desktop-header: Demo desktop images (x86_64): -->\n")
        os_name, entries = gendata.scan_confs()
        out = gendata.render_desktop(os_name, entries, gendata.parse_notes())
        self.assertEqual(
            out,
            "\n"
            "Demo desktop images (x86_64):\n"
            "\n"
            "| Release | x86_64 |\n"
            "|---------|---------|\n"
            "| 15.1-xfce | \u2705 |\n"
            "\n")

    def test_desktop_notes_appended(self):
        self.add("demo-15.1-xfce.conf",
                 conf_text("demo", "15.1-xfce", extra="hooks/xfce.sh"))
        write(gendata.DESKTOP_NOTES_PATH, "Some desktop prose.\n")
        os_name, entries = gendata.scan_confs()
        out = gendata.render_desktop(os_name, entries, gendata.parse_notes())
        self.assertTrue(out.endswith("Some desktop prose.\n"))


class TestShelved(GendataCase):
    # shelved: <tag> -- dropped from ALL FOUR outputs (kept on disk,
    # undocumented; the original "no-build" semantics before NEW-1 split
    # them into shelved vs no-build).
    def test_shelved_removed_from_all_release_and_json(self):
        self.add("demo-1.0.conf", conf_text("demo", "1.0"))
        self.add("demo-2.0.conf", conf_text("demo", "2.0"))
        write(gendata.NOTES_PATH, "<!-- shelved: 1.0 -->\n")
        outputs = gendata.generated_outputs()
        all_release = outputs[os.path.join("conf", "all.release.conf")]
        self.assertNotIn('"1.0"', all_release)
        self.assertIn('"2.0"', all_release)
        data = json.loads(
            outputs[os.path.join(".github", "data", "releases.json")])
        tags = [r["tag"] for r in data["releases"]]
        self.assertNotIn("1.0", tags)
        self.assertIn("2.0", tags)

    def test_table_row_vanishes_when_only_entry_shelved(self):
        self.add("demo-1.0.conf", conf_text("demo", "1.0"))
        self.add("demo-2.0.conf", conf_text("demo", "2.0"))
        write(gendata.NOTES_PATH, "<!-- shelved: 1.0 -->\n")
        outputs = gendata.generated_outputs()
        table = outputs[os.path.join(".github", "data", "table.md")]
        self.assertNotIn("| 1.0 |", table)
        self.assertIn("| 2.0 |", table)

    def test_table_cell_dash_when_one_arch_shelved(self):
        self.add("demo-1.0.conf", conf_text("demo", "1.0"))
        self.add("demo-1.0-aarch64.conf",
                 conf_text("demo", "1.0", arch="aarch64"))
        self.add("demo-2.0.conf", conf_text("demo", "2.0"))
        write(gendata.NOTES_PATH, "<!-- shelved: 1.0 -->\n")
        outputs = gendata.generated_outputs()
        table = outputs[os.path.join(".github", "data", "table.md")]
        self.assertIn(
            "| 1.0 | \u2014 | \u2705 (rsync,scp) |", table)

    def test_unknown_shelved_tag_fatal(self):
        self.add("demo-1.0.conf", conf_text("demo", "1.0"))
        write(gendata.NOTES_PATH, "<!-- shelved: 9.9 -->\n")
        with self.assertRaises(SystemExit):
            gendata.generated_outputs()

    def test_shelved_desktop_variant_not_in_desktop_md(self):
        # NEW-4 gap: a shelved desktop-variant tag must not render in
        # desktop.md, even though a sibling desktop variant still does.
        self.add("demo-15.1.conf", conf_text("demo", "15.1"))
        self.add("demo-15.1-xfce.conf",
                 conf_text("demo", "15.1-xfce", extra="hooks/xfce.sh"))
        self.add("demo-15.1-gnome.conf",
                 conf_text("demo", "15.1-gnome", extra="hooks/gnome.sh"))
        write(gendata.NOTES_PATH, "<!-- shelved: 15.1-xfce -->\n")
        outputs = gendata.generated_outputs()
        desktop = outputs[os.path.join(".github", "data", "desktop.md")]
        self.assertNotIn("15.1-xfce", desktop)
        self.assertIn("15.1-gnome", desktop)


class TestNoBuild(GendataCase):
    # no-build: <tag> -- dropped from ALL_RELEASES only. Still documented:
    # table.md/desktop.md keep a normal check+sync cell, and releases.json
    # keeps the entry but with "build": false (all other entries get
    # "build": true). Models "documented and tested elsewhere, but not
    # built by this repo's own matrix".
    def test_no_build_excluded_from_all_release_only(self):
        self.add("demo-1.0.conf", conf_text("demo", "1.0"))
        self.add("demo-2.0.conf", conf_text("demo", "2.0"))
        write(gendata.NOTES_PATH, "<!-- no-build: 1.0 -->\n")
        outputs = gendata.generated_outputs()
        all_release = outputs[os.path.join("conf", "all.release.conf")]
        self.assertNotIn('"1.0"', all_release)
        self.assertIn('"2.0"', all_release)

    def test_no_build_still_renders_in_table(self):
        self.add("demo-1.0.conf", conf_text("demo", "1.0"))
        self.add("demo-2.0.conf", conf_text("demo", "2.0"))
        write(gendata.NOTES_PATH, "<!-- no-build: 1.0 -->\n")
        outputs = gendata.generated_outputs()
        table = outputs[os.path.join(".github", "data", "table.md")]
        self.assertIn("| 1.0 | \u2705 (rsync,scp) |", table)
        self.assertIn("| 2.0 | \u2705 (rsync,scp) |", table)

    def test_no_build_json_build_field(self):
        self.add("demo-1.0.conf", conf_text("demo", "1.0"))
        self.add("demo-2.0.conf", conf_text("demo", "2.0"))
        write(gendata.NOTES_PATH, "<!-- no-build: 1.0 -->\n")
        outputs = gendata.generated_outputs()
        data = json.loads(
            outputs[os.path.join(".github", "data", "releases.json")])
        by_tag = dict((r["tag"], r) for r in data["releases"])
        self.assertFalse(by_tag["1.0"]["build"])
        self.assertTrue(by_tag["2.0"]["build"])

    def test_unknown_no_build_tag_fatal(self):
        self.add("demo-1.0.conf", conf_text("demo", "1.0"))
        write(gendata.NOTES_PATH, "<!-- no-build: 9.9 -->\n")
        with self.assertRaises(SystemExit):
            gendata.generated_outputs()


class TestMain(GendataCase):
    def test_write_then_check(self):
        self.add("demo-1.0.conf", conf_text("demo", "1.0"))
        self.assertEqual(gendata.main([]), 0)
        self.assertTrue(os.path.exists("conf/all.release.conf"))
        self.assertTrue(os.path.exists(".github/data/table.md"))
        self.assertTrue(os.path.exists(".github/data/releases.json"))
        self.assertFalse(os.path.exists(".github/data/desktop.md"))
        self.assertEqual(gendata.main(["--check"]), 0)
        self.add("demo-2.0.conf", conf_text("demo", "2.0"))
        self.assertEqual(gendata.main(["--check"]), 1)
        self.assertEqual(gendata.main([]), 0)
        self.assertEqual(gendata.main(["--check"]), 0)

    def test_deterministic(self):
        self.add("demo-1.0.conf", conf_text("demo", "1.0"))
        self.assertEqual(gendata.main([]), 0)
        with open(".github/data/table.md", encoding="utf-8") as f:
            first = f.read()
        self.assertEqual(gendata.main([]), 0)
        with open(".github/data/table.md", encoding="utf-8") as f:
            self.assertEqual(f.read(), first)

    def test_skip_sentinel_short_circuits(self):
        self.add("demo-1.0.conf", conf_text("demo", "1.0"))
        write(gendata.SKIP_PATH, "hand-maintained; gendata must do nothing\n")
        self.assertEqual(gendata.main([]), 0)
        self.assertFalse(os.path.exists("conf/all.release.conf"))
        self.assertFalse(os.path.exists(".github/data/table.md"))
        self.assertFalse(os.path.exists(".github/data/releases.json"))
        self.assertEqual(gendata.main(["--check"]), 0)


if __name__ == "__main__":
    unittest.main()
