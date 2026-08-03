#!/usr/bin/env python3
# gendata.py -- derive every release-list surface of an <os>-builder
# repo from its conf/ files (the single source of truth).
#
# Hand-owned inputs (the control layer, all under conf/ -- user rule:
# switches live in conf/, .github/data/ holds passive outputs only):
#   conf/<rel>[-<arch>].conf     one per release/arch (the inventory)
#   conf/all.release.conf        ALL_RELEASES -- WHICH confs build (the
#                                switch; a conf absent from this list is
#                                documented but not built)
# Hand-owned presentation input: .github/data/table.notes.md (footnotes,
# cosmetic labels, extra columns, shelved rows, url templates -- nothing
# in it changes what builds).
#
# Generated files (all overwritten in place):
#   .github/data/table.md        main release table (README render input)
#   .github/data/desktop.md      desktop-variant table (only if desktop confs)
#   .github/data/releases.json   machine-readable index ("build" mirrors
#                                all.release.conf membership)
#
# Run from a builder repo root:
#   python3 /path/to/base-builder/gendata.py [--check]

import argparse
import difflib
import json
import os
import re
import subprocess
import sys

CONF_DIR = "conf"
DATA_DIR = os.path.join(".github", "data")
NOTES_PATH = os.path.join(DATA_DIR, "table.notes.md")
DESKTOP_NOTES_PATH = os.path.join(DATA_DIR, "desktop.notes.md")
SKIP_PATH = os.path.join(DATA_DIR, "gendata.skip")

CANON_ARCHES = [
    "x86_64", "aarch64", "riscv64", "powerpc64", "sparc64",
    "s390x", "ppc64le", "loongarch64", "i386",
]

CHECK = "\u2705"  # white heavy check mark
DASH = "\u2014"   # em dash


def fatal(msg):
    sys.stderr.write("gendata: FATAL: %s\n" % msg)
    sys.exit(1)


def warn(msg):
    sys.stderr.write("gendata: WARNING: %s\n" % msg)


_STUB_BIN = None


def _stub_bin():
    """A PATH prefix whose curl/wget are instant no-ops.

    Sourcing a conf EXECUTES its command substitutions, and netbsd's
    confs deliberately run $(curl ...) in VM_INSTALL_CMD / VM_PKG_PATH
    (resolving the pkgsrc redirect on the HOST is the design -- build.py
    keeps the real curl for that). gendata never reads those keys, yet
    the 20 real curls against ftp.netbsd.org made every generate.yml
    Step 2, every watch run and every local --check ~70s slower. If a
    key gendata DOES read ever embedded a fetch, the stub collapses it
    to an empty string and the filename/emptiness validation catches
    that loudly rather than silently scanning stale network data.
    """
    global _STUB_BIN
    if _STUB_BIN is None:
        import tempfile
        d = tempfile.mkdtemp(prefix="gendata-stub-")
        for name in ("curl", "wget"):
            p = os.path.join(d, name)
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write("#!/bin/sh\nexit 0\n")
            os.chmod(p, 0o755)
        _STUB_BIN = d
    return _STUB_BIN


def source_conf(path):
    # Source the conf with bash, cwd = repo root, exactly like
    # build.py's conf_load, and print the variables gendata needs.
    # PATH is prefixed with the no-op curl/wget stubs -- see _stub_bin.
    script = (
        '. "$1" >/dev/null 2>&1; '
        'printf "%s\n%s\n%s\n%s\n%s\n%s\n" '
        '"$VM_OS_NAME" "$VM_RELEASE" "$VM_ARCH" '
        '"$VM_SYNC_METHODS" "$VM_EXTRA_SCRIPT" "$VM_SHUTDOWN_CMD"'
    )
    p = subprocess.run(["bash", "-c", script, "bash", path],
                       capture_output=True, text=True,
                       env={"PATH": _stub_bin() + os.pathsep
                            + os.environ.get("PATH", "/usr/bin:/bin")})
    if p.returncode != 0:
        fatal("sourcing %s failed: %s" % (path, p.stderr.strip()))
    vals = p.stdout.split("\n")
    if len(vals) < 6:
        fatal("sourcing %s produced no output" % path)
    return {
        "os_name": vals[0].strip(),
        "release": vals[1].strip(),
        "arch": vals[2].strip(),
        "sync": vals[3].strip(),
        "extra_script": vals[4].strip(),
        "shutdown_cmd": vals[5].strip(),
    }


def scan_confs():
    entries = []
    os_name = None
    for fn in sorted(os.listdir(CONF_DIR)):
        if not fn.endswith(".conf") or fn == "all.release.conf":
            continue
        v = source_conf(os.path.join(CONF_DIR, fn))
        if not v["os_name"] or not v["release"]:
            fatal("%s: VM_OS_NAME or VM_RELEASE empty" % fn)
        if os_name is None:
            os_name = v["os_name"]
        elif v["os_name"] != os_name:
            fatal("%s: VM_OS_NAME %r differs from %r"
                  % (fn, v["os_name"], os_name))
        arch = v["arch"] or "x86_64"
        suffix = "" if arch == "x86_64" else "-" + arch
        expect = "%s-%s%s.conf" % (os_name, v["release"], suffix)
        if fn != expect:
            fatal("%s: expected filename %s from VM_RELEASE=%r VM_ARCH=%r"
                  % (fn, expect, v["release"], v["arch"]))
        entries.append({
            "tag": fn[len(os_name) + 1:-len(".conf")],
            "release": v["release"],
            "arch": arch,
            "sync": v["sync"],
            "shutdown": v["shutdown_cmd"],
            "desktop": bool(v["extra_script"]),
        })
    if not entries:
        fatal("no conf files found under %s/" % CONF_DIR)
    return os_name, entries


def natural_key(s):
    key = []
    for tok in re.split(r"[.\-_]", s):
        # Split a mixed token like "r1beta10" into ["r", "1", "beta", "10"]
        # so the digit runs compare as numbers. Comparing the whole token
        # as a string ranks r1beta10 BELOW r1beta5, which would make the
        # upstream watcher decide "not newer" and go silently stale.
        for part in re.findall(r"\d+|\D+", tok):
            if part.isdigit():
                key.append((0, int(part), ""))
            else:
                key.append((1, 0, part.lower()))
    return key


def arch_rank(arch):
    if arch in CANON_ARCHES:
        return (CANON_ARCHES.index(arch), "")
    return (len(CANON_ARCHES), arch)


def base_release(entry):
    # Desktop tags embed the variant as the last hyphen token of
    # VM_RELEASE ("15.1-xfce" -> base "15.1").
    if not entry["desktop"]:
        return entry["release"]
    return entry["release"].rsplit("-", 1)[0]


def variant(entry):
    if not entry["desktop"]:
        return ""
    parts = entry["release"].rsplit("-", 1)
    return parts[1] if len(parts) > 1 else entry["release"]


def order_key(entry):
    return (natural_key(base_release(entry)),
            1 if entry["desktop"] else 0,
            arch_rank(entry["arch"]),
            variant(entry))


MEMBERSHIP_PATH = os.path.join(CONF_DIR, "all.release.conf")
ALL_RELEASES_RE = re.compile(r"^\s*ALL_RELEASES=")


def render_all_release(entries):
    """The canonical text for a freshly seeded membership file. NOT a
    generated output any more -- all.release.conf is the hand-owned build
    switch (see the header); this helper only exists for bootstrapping a
    brand-new repo and for the watcher's append formatting."""
    tags = [e["tag"] for e in sorted(entries, key=order_key)]
    return "ALL_RELEASES='%s'\n" % ", ".join('"%s"' % t for t in tags)


def parse_membership():
    """The hand-maintained build membership from conf/all.release.conf.

    Returns an ordered tag list, or None when the file is absent (bare
    test fixtures; every real repo has one) -- callers treat None as
    "every conf builds" and warn.
    """
    if not os.path.exists(MEMBERSHIP_PATH):
        warn("%s missing; treating every conf as buildable"
             % MEMBERSHIP_PATH)
        return None
    with open(MEMBERSHIP_PATH, "r", encoding="utf-8") as f:
        for line in f:
            if ALL_RELEASES_RE.match(line):
                return re.findall(r'"([^"]+)"', line)
    fatal("%s has no ALL_RELEASES= line" % MEMBERSHIP_PATH)


def render_releases_json(os_name, entries, notes, membership=None):
    member = (set(membership) if membership is not None
              else set(e["tag"] for e in entries))
    releases = []
    for e in sorted(entries, key=order_key):
        releases.append({
            "tag": e["tag"],
            "release": e["release"],
            "arch": e["arch"],
            "sync": e["sync"],
            "shutdown": e["shutdown"],
            "desktop": e["desktop"],
            "build": e["tag"] in member,
        })
    return json.dumps({"os": os_name, "releases": releases},
                      indent=2) + "\n"


DIRECTIVE_RE = re.compile(r"^<!--\s*([a-z-]+):\s*(.*?)\s*-->\s*$")
FOOTNOTE_RE = re.compile(r"^\[\^([^\]]+)\]:")


def parse_notes():
    notes = {
        "absent": {},
        "desktop_header": None,
        "force_main": set(),
        "force_desktop": set(),
        "arch_labels": {},
        "release_label": None,
        "extra_columns": [],
        "extra_values": {},
        "raw": "",
        "footnote_ids": set(),
        "shelved": set(),
        "url_templates": {},
    }
    if not os.path.exists(NOTES_PATH):
        return notes
    with open(NOTES_PATH, "r", encoding="utf-8") as f:
        notes["raw"] = f.read()
    for line in notes["raw"].splitlines():
        m = DIRECTIVE_RE.match(line.strip())
        if m:
            key, val = m.group(1), m.group(2)
            if key == "absent":
                parts = val.split()
                if len(parts) != 2:
                    fatal("notes: bad absent directive: %r" % line)
                notes["absent"][parts[0]] = parts[1]
            elif key == "desktop-header":
                notes["desktop_header"] = val
            elif key == "main-table":
                notes["force_main"].add(val)
            elif key == "desktop-table":
                notes["force_desktop"].add(val)
            elif key == "arch-label":
                if "=" not in val:
                    fatal("notes: bad arch-label directive: %r" % line)
                arch, label = val.split("=", 1)
                notes["arch_labels"][arch.strip()] = label.strip()
            elif key == "url-template":
                if "=" not in val:
                    fatal("notes: bad url-template directive: %r" % line)
                var, tpl = val.split("=", 1)
                notes["url_templates"][var.strip()] = tpl.strip()
            elif key == "release-label":
                notes["release_label"] = val
            elif key == "extra-column":
                notes["extra_columns"].append(val)
            elif key == "extra-value":
                parts = val.split(None, 1)
                if len(parts) != 2:
                    fatal("notes: bad extra-value directive: %r" % line)
                if not notes["extra_columns"]:
                    fatal("notes: extra-value before any extra-column")
                col = notes["extra_columns"][-1]
                notes["extra_values"][(col, parts[0])] = parts[1]
            elif key == "shelved":
                notes["shelved"].add(val)
            elif key == "no-build":
                # The build switch moved OUT of the notes (user rule:
                # switches live in conf/, data/ is passive): a conf
                # builds IFF its tag is listed in conf/all.release.conf.
                fatal("notes: the no-build directive is gone -- remove "
                      "%r and instead leave the tag out of "
                      "conf/all.release.conf" % line)
            else:
                fatal("notes: unknown directive %r" % line)
            continue
        m = FOOTNOTE_RE.match(line)
        if m:
            notes["footnote_ids"].add(m.group(1))
    for combo, fid in sorted(notes["absent"].items()):
        if fid not in notes["footnote_ids"]:
            fatal("notes: absent %s references undefined footnote id %s"
                  % (combo, fid))
    return notes


def apply_overrides(entries, notes):
    for e in entries:
        if e["tag"] in notes["force_main"]:
            e["desktop"] = False
        elif e["tag"] in notes["force_desktop"]:
            e["desktop"] = True


def table_arches(entries):
    present = set(e["arch"] for e in entries)
    ordered = [a for a in CANON_ARCHES if a in present]
    return ordered + sorted(a for a in present if a not in CANON_ARCHES)


def render_table(entries, notes):
    main = [e for e in entries if not e["desktop"]]
    arches = table_arches(entries)
    cells = dict(((e["release"], e["arch"]), e) for e in main)
    releases = sorted(set(e["release"] for e in main),
                      key=lambda r: (natural_key(r), r), reverse=True)
    cols = ([notes["release_label"] or "Release"]
            + notes["extra_columns"]
            + [notes["arch_labels"].get(a, a) for a in arches])
    lines = ["", ""]
    lines.append("| " + " | ".join(cols) + " |")
    lines.append("|" + "|".join("---------" for _ in cols) + "|")
    for rel in releases:
        row = [rel]
        for col in notes["extra_columns"]:
            row.append(notes["extra_values"].get((col, rel), DASH))
        for arch in arches:
            e = cells.get((rel, arch))
            combo = "%s-%s" % (rel, arch)
            if e is not None:
                if combo in notes["absent"]:
                    warn("stale absent footnote for %s (conf exists)" % combo)
                if e["sync"]:
                    row.append("%s (%s)" % (CHECK, e["sync"]))
                else:
                    warn("%s: VM_SYNC_METHODS empty" % e["tag"])
                    row.append(CHECK)
            else:
                fid = notes["absent"].get(combo)
                row.append(DASH + ("[^%s]" % fid if fid else ""))
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    text = "\n".join(lines) + "\n"
    if notes["raw"]:
        text += notes["raw"]
        if not text.endswith("\n"):
            text += "\n"
    return text


def desktop_sort_key(tag):
    parts = tag.rsplit("-", 1)
    return (natural_key(parts[0]), parts[1] if len(parts) > 1 else "")


def render_desktop(os_name, entries, notes):
    desk = [e for e in entries if e["desktop"]]
    if not desk:
        return None
    arches = table_arches(entries)
    cells = dict(((e["release"], e["arch"]), e) for e in desk)
    tags = sorted(set(e["release"] for e in desk),
                  key=lambda t: (desktop_sort_key(t), t), reverse=True)
    header = notes["desktop_header"] or ("%s desktop images:" % os_name)
    cols = ([notes["release_label"] or "Release"]
            + [notes["arch_labels"].get(a, a) for a in arches])
    lines = ["", header, ""]
    lines.append("| " + " | ".join(cols) + " |")
    lines.append("|" + "|".join("---------" for _ in cols) + "|")
    for tag in tags:
        row = [tag]
        for arch in arches:
            row.append(CHECK if (tag, arch) in cells else DASH)
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    text = "\n".join(lines) + "\n"
    if os.path.exists(DESKTOP_NOTES_PATH):
        with open(DESKTOP_NOTES_PATH, "r", encoding="utf-8") as f:
            raw = f.read()
        text += raw
        if not text.endswith("\n"):
            text += "\n"
    return text


def generated_outputs():
    os_name, entries = scan_confs()
    notes = parse_notes()
    apply_overrides(entries, notes)
    all_tags = set(e["tag"] for e in entries)
    unknown_shelved = notes["shelved"] - all_tags
    if unknown_shelved:
        fatal("notes: shelved tag(s) not found among conf entries: %s"
              % ", ".join(sorted(unknown_shelved)))
    membership = parse_membership()
    if membership is not None:
        unknown_members = set(membership) - all_tags
        if unknown_members:
            fatal("%s lists tag(s) with no conf file: %s"
                  % (MEMBERSHIP_PATH, ", ".join(sorted(unknown_members))))
        shelved_members = set(membership) & notes["shelved"]
        if shelved_members:
            fatal("%s lists shelved tag(s): %s -- a shelved conf must not "
                  "build" % (MEMBERSHIP_PATH,
                             ", ".join(sorted(shelved_members))))
    # shelved: dropped from every output (kept on disk, undocumented).
    # A conf ABSENT from all.release.conf stays documented (its table row
    # and releases.json entry remain, with "build": false) -- the
    # membership file only switches the build off.
    active_entries = [e for e in entries if e["tag"] not in notes["shelved"]]
    outputs = {
        os.path.join(DATA_DIR, "table.md"):
            render_table(active_entries, notes),
        os.path.join(DATA_DIR, "releases.json"):
            render_releases_json(os_name, active_entries, notes,
                                 membership),
    }
    desktop = render_desktop(os_name, active_entries, notes)
    if desktop is not None:
        outputs[os.path.join(DATA_DIR, "desktop.md")] = desktop
    return outputs


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="generate release-list files from conf/*.conf")
    ap.add_argument("--check", action="store_true",
                    help="verify files match; write nothing; rc 1 on diff")
    args = ap.parse_args(argv)
    if os.path.exists(SKIP_PATH):
        warn("gendata.skip present; nothing generated")
        return 0
    if not os.path.isdir(CONF_DIR):
        fatal("no conf/ directory here; run from a builder repo root")
    outputs = generated_outputs()
    desktop_path = os.path.join(DATA_DIR, "desktop.md")
    if desktop_path not in outputs and os.path.exists(desktop_path):
        warn("desktop.md exists but no desktop confs found; left untouched")
    differs = False
    for path in sorted(outputs):
        content = outputs[path]
        old = None
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                old = f.read()
        if old == content:
            continue
        differs = True
        if args.check:
            sys.stderr.write("gendata: %s differs:\n" % path)
            sys.stderr.writelines(difflib.unified_diff(
                (old or "").splitlines(True), content.splitlines(True),
                "current/" + path, "generated/" + path))
        else:
            d = os.path.dirname(path)
            if d and not os.path.isdir(d):
                os.makedirs(d)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
            print("gendata: wrote %s" % path)
    if args.check:
        if not differs:
            print("gendata: all generated files up to date")
        return 1 if differs else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
