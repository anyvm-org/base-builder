#!/usr/bin/env python3
# watch.py -- track a builder's upstream and land the confs for a new
# release, so the generation layer (gendata, run by generate.yml) can turn
# it into a build-matrix entry by itself.
#
# Run from a builder repo root, with base-builder cloned alongside:
#   python3 <base-builder>/watch.py [--check]
#
# --check prints what it would write and never touches the disk.
#
# The per-builder detection hook (hooks/upstream_check.py) only has to
# print the latest upstream version, in the same form the confs use for
# VM_RELEASE. Empty output means "nothing detected"; a non-zero exit means
# detection is broken and must be reported, never swallowed.

import argparse
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

_sleep = time.sleep

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gendata

HOOK_PATH = os.path.join("hooks", "upstream_check.py")
NO_HOOK = -1
URL_KEYS = ("VM_ISO_LINK", "VM_VHD_LINK")
# Keys whose versioned companion file may be copied to the new release.
# VM_OPTS is an installer answer file (openbsd's .resp, netbsd's and
# openindiana's .opts.txt): the version appears in it as a path or a
# prompt value, and rewriting it is exactly the intent. Every other key
# points at content the engine cannot reason about -- see check_companions.
COMPANION_KEYS = ("VM_OPTS",)
ASSIGN_RE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z_0-9]*)=(.*)$")


def log(msg):
    sys.stdout.write("watch: %s\n" % msg)


def warn(msg):
    sys.stderr.write("watch: WARNING: %s\n" % msg)


def fatal(msg):
    sys.stderr.write("watch: FATAL: %s\n" % msg)
    sys.exit(1)


def detect_upstream():
    """Run the detection hook. Returns (version, rc).

    version is "" when the hook is missing, prints nothing, or fails.
    rc is NO_HOOK when there is no hook at all, otherwise the hook's exit
    code -- the caller distinguishes "nothing to do" from "broken".
    """
    if not os.path.exists(HOOK_PATH):
        return ("", NO_HOOK)
    # Strip VM_* so a stray exported build variable cannot steer detection,
    # but keep the rest of the environment. A bare {"PATH": ...} (what
    # gendata.source_conf uses, correctly, for a conf it SOURCES) breaks a
    # hook that opens an HTTPS connection: on Windows the interpreter needs
    # SYSTEMROOT to reach the crypto provider, and every fetch dies with an
    # opaque "[SSL] unknown error". CI never saw it -- Linux does not need
    # the variable -- so it would only ever have bitten a maintainer
    # testing a new hook locally.
    env = dict(os.environ)
    for k in [k for k in env if k.startswith("VM_")]:
        del env[k]
    python = sys.executable or "python3"
    p = subprocess.run([python, HOOK_PATH], capture_output=True, text=True,
                       env=env)
    if p.returncode != 0:
        warn("%s exited %d: %s" % (HOOK_PATH, p.returncode,
                                   p.stderr.strip()[:400]))
        return ("", p.returncode)
    version = p.stdout.strip().splitlines()
    version = version[0].strip() if version else ""
    return (version, 0)


def real_bases(entries):
    """The releases that are releases, not variants dressed as one.

    gendata's `desktop` flag is `bool(VM_EXTRA_SCRIPT)`, which only
    catches variants built by a post-install hook. Several builders ship
    a variant as an ordinary conf with no hook at all -- omnios/
    openindiana "-build" (same media, extra packages), solaris
    "11.4-gcc-14" (toolchain), ghostbsd "26.1-xfce" (a different live
    ISO) -- so gendata reports "r151058-build" as a release of its own,
    and natural_key ranks it ABOVE "r151058".

    The rule here: a release that is another release plus "-<suffix>" is
    a variant of it. Checked against every conf in the fleet on
    2026-07-27 -- every hyphen-extension pair that exists today is a
    variant pair, and the names that merely look like one (openEuler's
    "22.03-LTS-SP4", "24.03-LTS-SP1") extend no other conf's release, so
    they stay real.
    """
    rels = set(e["release"] for e in entries if not e["desktop"])
    return set(r for r in rels
               if not any(o != r and r.startswith(o + "-") for o in rels))


def watch_base(entry, bases):
    """The real release `entry` belongs to, for template/replication.

    Resolves straight to a member of `bases`, so a chain of variants
    (solaris 11.4 -> 11.4-gcc -> 11.4-gcc-14) collapses to the release
    at the bottom rather than to the intermediate variant.
    """
    rel = gendata.base_release(entry)   # strips a VM_EXTRA_SCRIPT variant
    if rel in bases:
        return rel
    cands = [b for b in bases if rel.startswith(b + "-")]
    return max(cands, key=len) if cands else rel


def _rc_token_re(version):
    """The version pinned to release-candidate media: '11.0_RC7' or
    '11.0-RC7' style, exactly for this version."""
    return re.compile(re.escape(version) + r"[_-]RC\d+")


def _conf_urls(os_name, tag):
    """The URL-key values of one conf file, read fresh from disk."""
    path = os.path.join(gendata.CONF_DIR, "%s-%s.conf" % (os_name, tag))
    urls = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            m = ASSIGN_RE.match(line.rstrip("\n"))
            if m and m.group(2) in URL_KEYS:
                urls.append(_strip_quotes(m.group(3))[0])
    return urls


def decide(os_name, entries, notes, version):
    """Return (action, arg). action is "none", "new" (arg = template
    release) or "refresh" (arg = version whose confs pin RC media)."""
    if not version:
        return ("none", None)
    # Exact match only: notes["shelved"]/notes["no_build"] hold conf TAGS,
    # which may carry an arch/variant suffix (e.g. "7.9-xfce-aarch64").
    # A shelved VARIANT must not suppress the whole release -- only that
    # one tag is hidden, and plan_files() already skips shelved variants
    # individually when replicating. Matching a bare version as a prefix
    # of a suffixed tag used to treat "16.0-aarch64 shelved" as "16.0
    # already covered", which wrongly refused the entire 16.0 release.
    if version in notes["shelved"] or version in notes["no_build"]:
        log("%s is listed in table.notes.md, nothing to do" % version)
        return ("none", None)
    reals = real_bases(entries)
    for e in entries:
        if (e["release"] == version or gendata.base_release(e) == version
                or watch_base(e, reals) == version):
            # The version is covered -- but maybe by RELEASE-CANDIDATE
            # media. netbsd kept 11.0 in RC status so long that its confs
            # pinned the 11.0_RC7 URLs; when the final NetBSD-11.0/
            # directory appeared, "already has a conf" silently ignored
            # it. The hook only reports FINAL releases, so a hook version
            # whose conf URLs still carry an RC token means the final
            # media now exists and the URLs must move.
            rc = _rc_token_re(version)
            stale = [x["tag"] for x in entries
                     if gendata.base_release(x) == version
                     and any(rc.search(u) for u in
                             _conf_urls(os_name, x["tag"]))]
            if stale:
                log("%s conf(s) still pin RC media (%s), refreshing"
                    % (version, ", ".join(stale)))
                return ("refresh", version)
            log("%s already has a conf, nothing to do" % version)
            return ("none", None)
    # Shelved entries are excluded from the template candidates too -- a
    # shelved conf being the numerically newest must not make plan_files
    # replicate it (it would just skip it and hand back an odd/empty plan).
    bases = [watch_base(e, reals) for e in entries
             if not e["desktop"] and e["tag"] not in notes["shelved"]]
    if not bases:
        fatal("no non-desktop conf to use as a template")
    newest = sorted(set(bases), key=gendata.natural_key)[-1]
    if gendata.natural_key(version) <= gendata.natural_key(newest):
        log("%s is not newer than %s, nothing to do" % (version, newest))
        return ("none", None)
    return ("new", newest)


def compress(version):
    return "".join(c for c in version if c.isalnum())


def substitute_value(key, value, old, new):
    if key == "VM_RELEASE":
        # Replace the release token in place instead of overwriting the
        # whole field: a variant conf's VM_RELEASE carries a suffix
        # ("15.1-xfce", "r151058-build") that the new FILENAME keeps, and
        # gendata.scan_confs() fatals when the two disagree -- which would
        # freeze that repo's entire generate.yml chain, not just one build.
        return value.replace(old, new, 1)
    out = value.replace(old, new)
    co, cn = compress(old), compress(new)
    if co and co != old:
        out = out.replace(co, cn)
    return out


def _standalone_at(text, idx, token):
    """True when the occurrence of `token` starting at `idx` in `text` is
    not glued to an alphanumeric on either side (a string edge counts as
    a boundary, same as an explicit non-alphanumeric character).

    Shared by substitute_boundary (what is safe to rewrite) and the
    leftover scan in plan_files (what is worth warning about), so both
    agree on what counts as a real version reference versus a
    coincidental digit run inside a hash or other opaque blob.
    """
    before = text[idx - 1] if idx > 0 else ""
    after_i = idx + len(token)
    after = text[after_i] if after_i < len(text) else ""
    return not ((before and before.isalnum()) or (after and after.isalnum()))


def substitute_boundary(text, old, new):
    """Replace `old` with `new` only where it is not glued to alphanumerics.

    Companion answer files carry values that must never be touched -- a
    bcrypt root-password hash, sysinst keystroke timeouts -- so the loose
    substring replace used for URLs is unsafe here. A version in a path
    ("pub/OpenBSD/7.9/amd64") is delimited by non-alphanumerics; a
    coincidental digit run inside a hash is not.
    """
    out, i = [], 0
    while True:
        j = text.find(old, i)
        if j < 0:
            out.append(text[i:])
            return "".join(out)
        out.append(text[i:j])
        out.append(new if _standalone_at(text, j, old) else old)
        i = j + len(old)


def _new_version_spans(text, new):
    """Character ranges already occupied by the new version string.

    When `new` starts with `old` plus a delimiter (openEuler's
    "24.03-LTS-SP1" style), a freshly rewritten occurrence would
    otherwise look like a leftover and warn about itself.
    """
    spans, i = [], 0
    while True:
        j = text.find(new, i)
        if j < 0:
            return spans
        spans.append((j, j + len(new)))
        i = j + len(new)


def _standalone_lines(text, token):
    """1-based line numbers where `token` appears as a standalone
    occurrence (not glued to alphanumerics) in `text`."""
    lines, i = set(), 0
    while True:
        j = text.find(token, i)
        if j < 0:
            return lines
        if _standalone_at(text, j, token):
            lines.add(text.count("\n", 0, j) + 1)
        i = j + len(token)


def _strip_quotes(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1], v[0]
    return v, ""


def _looks_like_repo_path(v):
    return v.startswith("conf/") or v.startswith("hooks/") or (
        "/" not in v and v.endswith((".txt", ".resp", ".sh", ".opts")))


def plan_files(os_name, entries, notes, old, new):
    """Build the list of files to write for release `new`, modelled on
    every conf of release `old`. Never writes anything."""
    plan, extra_copies = [], []
    reals = real_bases(entries)
    for e in entries:
        if watch_base(e, reals) != old:
            continue
        # shelved: hidden everywhere; no-build: deliberately out of the
        # build matrix. Replicating either onto the new release silently
        # switches it back on, because the NEW tag is on neither list.
        if e["tag"] in notes["shelved"] or e["tag"] in notes["no_build"]:
            log("%s-%s.conf is shelved or no-build, not replicating"
                % (os_name, e["tag"]))
            continue
        src = os.path.join(gendata.CONF_DIR,
                           "%s-%s.conf" % (os_name, e["tag"]))
        with open(src, "r", encoding="utf-8") as f:
            src_text = f.read()
        new_tag = e["tag"].replace(old, new, 1)
        dst = os.path.join(gendata.CONF_DIR,
                           "%s-%s.conf" % (os_name, new_tag))
        out_lines, urls, url_pairs = [], [], []
        for line in src_text.splitlines(True):
            m = ASSIGN_RE.match(line.rstrip("\n"))
            if not m:
                out_lines.append(line)
                continue
            indent, key, raw = m.group(1), m.group(2), m.group(3)
            value, quote = _strip_quotes(raw)
            # Only fields known to carry the version get substituted --
            # VM_RELEASE itself, the download-URL fields, and repo-relative
            # paths (answer files etc, tested on the ORIGINAL value, since
            # substituting first would corrupt the same check below).
            # Every other assignment is copied through untouched, so a
            # version fragment can never corrupt an unrelated value (e.g.
            # old=7.9 new=8.0 must not turn "mirror79.example.com" into
            # "mirror80.example.com", nor "17.90" into "18.00").
            if key == "VM_RELEASE" or key in URL_KEYS or _looks_like_repo_path(value):
                newval = substitute_value(key, value, old, new)
            else:
                newval = value
            if key in URL_KEYS and key in notes["url_templates"]:
                # {VC} is expanded before {V} defensively only: with the
                # current brace syntax "{V}" is not a substring of "{VC}",
                # so either order gives the same result. Keep this order in
                # case the placeholders ever lose their braces, where it
                # WOULD matter.
                newval = (notes["url_templates"][key]
                          .replace("{VC}", compress(new))
                          .replace("{V}", new))
            # NOTE: the URL substitution above is a heuristic. Its compressed
            # -form replace (see substitute_value/compress) cannot tell a
            # real version fragment in a URL apart from a coincidentally
            # matching digit run, so a URL string could still be mangled.
            # That is exactly why the URL HEAD gate (a later task) exists as
            # the real safety net before any generated conf is trusted.
            if _looks_like_repo_path(newval) and newval != value:
                # a path that does not exist after substitution means the
                # conf deliberately shares an older file -- keep it as is
                if not os.path.exists(newval):
                    if os.path.exists(value):
                        extra_copies.append((key, value, newval))
                    else:
                        newval = value
            if key in URL_KEYS:
                url_pairs.append((key, value, newval))
                if newval:
                    urls.append(newval)
            out_lines.append("%s%s=%s%s%s\n"
                             % (indent, key, quote, newval, quote))
        plan.append({"path": dst, "content": "".join(out_lines),
                     "urls": urls, "url_pairs": url_pairs, "source": src,
                     "companion_key": None, "leftovers": []})
    for key, src, dst in extra_copies:
        if any(p["path"] == dst for p in plan):
            continue
        with open(src, "r", encoding="utf-8") as f:
            text = f.read()
        # Companion files (openbsd .resp, netbsd .opts.txt) can carry a
        # bcrypt root-password hash or a sysinst keystroke timeout right
        # next to a version-bearing path -- a blind whole-text replace
        # (dotted AND compressed form) could silently corrupt either one.
        # Only the dotted version is substituted here, and only at
        # non-alphanumeric boundaries; the compressed form is never
        # touched in a companion file.
        text = substitute_boundary(text, old, new)
        # (companion files carry no download URL; url_pairs stays empty)
        # Warn only on a STANDALONE leftover, using the same boundary rule
        # substitute_boundary used to decide what was safe to rewrite. A
        # digit run glued to alphanumerics (inside a hash, base64 blob,
        # etc) is not a real version reference and must not be flagged --
        # doing so would just teach people to ignore the warning. A bare
        # leftover token sitting between delimiters (a dotted version
        # substitute_boundary could not rewrite, or a compressed-form
        # token, which is never substituted in a companion file) is a
        # genuine miss and must still be reported. Skip any match that
        # falls inside a freshly-written new version string.
        co = compress(old)
        new_spans = _new_version_spans(text, new)
        leftover_lines = set()
        i = 0
        while True:
            j = text.find(old, i)
            if j < 0:
                break
            if (_standalone_at(text, j, old) and
                    not any(s <= j and j + len(old) <= e
                            for s, e in new_spans)):
                leftover_lines.add(text.count("\n", 0, j) + 1)
            i = j + len(old)
        if co and co != old:
            i = 0
            while True:
                j = text.find(co, i)
                if j < 0:
                    break
                if (_standalone_at(text, j, co) and
                        not any(s <= j and j + len(co) <= e
                                for s, e in new_spans)):
                    leftover_lines.add(text.count("\n", 0, j) + 1)
                i = j + len(co)
        for lineno in sorted(leftover_lines):
            warn("%s:%d: still contains %s (or its compressed form) "
                 "after substitution" % (dst, lineno, old))
        plan.append({"path": dst, "content": text, "urls": [],
                     "url_pairs": [], "source": src,
                     "companion_key": key,
                     "leftovers": sorted(leftover_lines)})
    return plan


def check_companions(plan):
    """Refuse to copy a versioned companion the engine cannot reason about.

    Nothing inspects a companion's CONTENT -- not the filename gate, not
    the url-moved gate, not the HEAD check. Copying one is therefore an
    act of faith, and it is only justified for a file whose relationship
    to the version is mechanical.

    VM_OPTS qualifies: it is an installer answer file, the version shows
    up in it as a download path or a typed prompt value, and rewriting
    that is the entire point (openbsd's .resp, netbsd's and openindiana's
    .opts.txt). VM_INSTALL_SCRIPT does not: build.py pipes it into the
    guest shell as code. haiku is the live case --
    conf/haiku-r1beta5-pinned-sshfs.sh pins .hpkg FILENAMES
    ("glib2-2.78.0-2-x86_64.hpkg") that are beta5-era packages with no
    relation to the OS version string, and its own header says "a future
    r1beta6 conf must NOT reuse it". Copying it rewrote the header to say
    r1beta6 while leaving the beta5 pins in place, on a green run.

    A surviving version token is refused for the same reason: the
    substitution demonstrably did not cover the file, so the copy is
    known-wrong rather than merely unverified. Warning about it was not
    enough -- the run stayed green, so no issue opened and the message
    sat unread in a successful log.
    """
    bad = []
    for item in plan:
        key = item.get("companion_key")
        if not key:
            continue
        base = os.path.basename(item["path"])
        if key not in COMPANION_KEYS:
            bad.append("%s: copied from %s via %s, which is not an answer "
                       "file (%s only) -- land this release by hand"
                       % (base, item["source"].replace(os.sep, "/"), key,
                          "/".join(COMPANION_KEYS)))
        elif item.get("leftovers"):
            bad.append("%s: version token survives substitution on line(s) "
                       "%s" % (base, ", ".join(str(n)
                                               for n in item["leftovers"])))
    return bad


def plan_refresh(os_name, entries, version):
    """Rewrite RC-media URLs to the final release's, in place.

    Same-named files, URL-key lines only: the RC token
    ('<version>[_-]RC<n>') collapses to the bare version and every other
    byte of the conf survives -- a non-URL value that merely LOOKS like
    an RC reference is data, not a pin. Companion files referenced by
    the conf get the same token collapsed at non-alnum boundaries when
    they carry it (netbsd's answer files match on version-free installer
    text, so in practice only comments move). Confs whose URLs are
    already final produce no plan item, which is what makes the action
    idempotent -- the day after a refresh lands, decide() reports plain
    "already has a conf".
    """
    rc = _rc_token_re(version)
    plan = []
    for e in entries:
        if gendata.base_release(e) != version:
            continue
        path = os.path.join(gendata.CONF_DIR,
                            "%s-%s.conf" % (os_name, e["tag"]))
        with open(path, "r", encoding="utf-8", newline="") as f:
            text = f.read()
        out_lines, urls, url_pairs, hit = [], [], [], False
        for line in text.splitlines(True):
            m = ASSIGN_RE.match(line.rstrip("\r\n"))
            if m and m.group(2) in URL_KEYS:
                indent, key, raw = m.group(1), m.group(2), m.group(3)
                value, quote = _strip_quotes(raw)
                newval = rc.sub(version, value)
                if newval != value:
                    hit = True
                    eol = "\r\n" if line.endswith("\r\n") else "\n"
                    line = "%s%s=%s%s%s%s" % (indent, key, quote, newval,
                                              quote, eol)
                    # only the CHANGED urls enter the gates: a second URL
                    # key without an RC token is legitimately unchanged
                    # here, and check_urls_moved would misread it
                    url_pairs.append((key, value, newval))
                    urls.append(newval)
            out_lines.append(line)
        if hit:
            plan.append({"path": path, "content": "".join(out_lines),
                         "urls": urls, "url_pairs": url_pairs,
                         "source": path, "companion_key": None,
                         "leftovers": []})
    return plan


def check_filenames(os_name, plan):
    """Re-derive each planned conf's filename from its own content.

    This mirrors gendata.scan_confs()'s filename rule. gendata runs as the
    FIRST step of generate.yml, and it calls fatal() on a mismatch -- so a
    conf the watcher lands with the wrong VM_RELEASE does not merely fail
    one image build, it stops build.py, the tables and the matrix from
    propagating into that repo until a human fixes it. Failing the watcher
    run instead keeps the damage in one retryable workflow.
    """
    bad = []
    for item in plan:
        base = os.path.basename(item["path"])
        if not base.endswith(".conf"):
            continue
        release, arch = "", ""
        for line in item["content"].splitlines():
            m = ASSIGN_RE.match(line)
            if not m:
                continue
            if m.group(2) == "VM_RELEASE":
                release = _strip_quotes(m.group(3))[0]
            elif m.group(2) == "VM_ARCH":
                arch = _strip_quotes(m.group(3))[0]
        arch = arch or "x86_64"
        suffix = "" if arch == "x86_64" else "-" + arch
        expect = "%s-%s%s.conf" % (os_name, release, suffix)
        if base != expect:
            bad.append("%s: VM_RELEASE=%s VM_ARCH=%s implies %s"
                       % (base, release, arch, expect))
    return bad


def check_urls_moved(plan):
    """Every planned conf must carry a download URL, and it must have
    moved off the template's release.

    The HEAD gate can only catch a URL it can see change. A conf that
    keeps the template's URL byte-identical claims the new release while
    pointing at the OLD media -- and the gate happily returns 200,
    because that media is still published. That is the one failure the
    network check is structurally blind to, so it has to be caught here.
    A conf with no URL field at all is the same blindness with no
    substitution involved.

    Surveyed across all 13 watchable builders on 2026-07-27: every
    planned conf has a URL and every URL moves. Both are invariants of
    the real data, not aspirations -- if one ever trips, the release
    genuinely cannot be derived and needs a url-template line in that
    builder's table.notes.md (or a hand-written conf).
    """
    bad = []
    for item in plan:
        if not item["path"].endswith(".conf"):
            continue
        pairs = item.get("url_pairs", [])
        base = os.path.basename(item["path"])
        if not pairs:
            bad.append("%s: no %s field -- nothing for the URL gate to "
                       "check" % (base, "/".join(URL_KEYS)))
            continue
        for key, before, after in pairs:
            if before == after:
                bad.append("%s: %s did not move off the template (%s) -- "
                           "add a url-template line to table.notes.md"
                           % (base, key, before))
    return bad


class UrlError(Exception):
    pass


def _head(url):
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        # some mirrors reject HEAD outright -- retry as a 1-byte GET
        if e.code in (403, 405, 501):
            req = urllib.request.Request(url)
            req.add_header("Range", "bytes=0-0")
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return r.status
            except Exception as e2:
                raise UrlError(str(e2))
        raise UrlError("HTTP %d" % e.code)
    except Exception as e:
        raise UrlError(str(e))


def check_url(url, opener=None, attempts=3, sleep=_sleep):
    """HEAD the URL, retrying a TRANSIENT failure a couple of times.

    A gate failure fails the run and opens an issue, so a single blip on
    one of 13 upstream sites (several are small self-hosted mirrors)
    would file a bug report about a release that is perfectly fine -- and
    that stale issue then suppresses the report for a real break, since
    the workflow posts only when no issue is open.

    Only transport-level failures are retried. An HTTP status is upstream
    answering clearly: a 404 means the derived URL is wrong and will stay
    wrong, so retrying it just delays the report.
    """
    fn = opener or _head
    last = ""
    for attempt in range(attempts):
        try:
            fn(url)
            return (True, "")
        except UrlError as e:
            last = str(e)
            if last.startswith("HTTP "):
                return (False, last)
            if attempt + 1 < attempts:
                sleep(2 * (attempt + 1))
    return (False, last)


def verify(plan, opener=None):
    bad = []
    for item in plan:
        for url in item.get("urls", []):
            ok, detail = check_url(url, opener)
            if not ok:
                bad.append("%s: %s -> %s" % (item["path"], url, detail))
    return bad


_TEST_OPENER = None   # tests inject a fake HEAD; never set in production


def suggest_allow_lines(os_name, plan, allow_path):
    """Mirror existing coverage.allow entries onto the new tags.

    A variant suffix that is already allow-listed (the deliberately
    untested -build / toolchain images) needs the same line for the new
    release, or the coverage gate goes red the moment the image ships.
    """
    if not os.path.exists(allow_path):
        return []
    existing = []
    with open(allow_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) == 3 and parts[0] == os_name and parts[1] != "*":
                existing.append((parts[1], parts[2]))
    out = []
    for item in plan:
        base = os.path.basename(item["path"])
        if not base.endswith(".conf"):
            continue
        tag = base[len(os_name) + 1:-len(".conf")]
        for old_tag, wf in existing:
            suffix = old_tag.split("-", 1)[1] if "-" in old_tag else ""
            if suffix and tag.endswith("-" + suffix):
                line = "%s %s %s" % (os_name, tag, wf)
                if line not in out:
                    out.append(line)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="land confs for a new upstream release")
    ap.add_argument("--check", action="store_true",
                    help="print the plan; write nothing")
    ap.add_argument("--allow-file",
                    default=os.path.join("..", "anyvm", ".github",
                                         "coverage.allow"),
                    help="anyvm coverage.allow, for suggested lines")
    ap.add_argument("--allow-out",
                    help="write the suggested coverage.allow lines here, "
                         "so the workflow can put them in the commit "
                         "message (nothing is written when there are none)")
    args = ap.parse_args(argv)
    if not os.path.isdir(gendata.CONF_DIR):
        fatal("no conf/ directory here; run from a builder repo root")

    version, rc = detect_upstream()
    if rc == NO_HOOK:
        log("no %s, nothing to watch" % HOOK_PATH)
        return 0
    if rc != 0:
        sys.stderr.write("watch: detection hook failed (rc=%d)\n" % rc)
        return 1
    if not version:
        log("hook reported no version, nothing to do")
        return 0
    log("upstream reports %s" % version)

    os_name, entries = gendata.scan_confs()
    notes = gendata.parse_notes()
    gendata.apply_overrides(entries, notes)
    action, template = decide(os_name, entries, notes, version)
    if action == "none":
        return 0

    if action == "refresh":
        plan = plan_refresh(os_name, entries, version)
        if not plan:
            log("nothing to refresh after all")
            return 0
        log("refreshing %d conf(s) from RC to final %s media"
            % (len(plan), version))
    else:
        plan = plan_files(os_name, entries, notes, template, version)
        if not plan:
            fatal("nothing to replicate from %s" % template)
        log("modelling %s on %s (%d file(s))"
            % (version, template, len(plan)))

    # The watcher workflow commits with `git add conf`, so anything written
    # elsewhere would be created on the runner, never committed, and leave
    # the conf that references it pointing at a file that does not exist on
    # main. Refuse rather than land a dangling reference.
    # normpath first: a conf value like "conf/../hooks/x.sh" is a repo path
    # by _looks_like_repo_path's rule and would sail past a plain
    # startswith("conf/") check while landing outside conf/.
    outside = [p["path"] for p in plan
               if not os.path.normpath(p["path"]).replace(
                   os.sep, "/").startswith(gendata.CONF_DIR + "/")]
    if outside:
        sys.stderr.write("watch: planned file(s) outside %s/, refusing:\n"
                         % gendata.CONF_DIR)
        for p in outside:
            sys.stderr.write("  %s\n" % p)
        return 1

    named = check_filenames(os_name, plan)
    if named:
        sys.stderr.write("watch: planned conf(s) gendata would reject, "
                         "nothing written:\n")
        for b in named:
            sys.stderr.write("  %s\n" % b)
        return 1

    stuck = check_urls_moved(plan)
    if stuck:
        sys.stderr.write("watch: planned conf(s) the URL gate cannot "
                         "check, nothing written:\n")
        for b in stuck:
            sys.stderr.write("  %s\n" % b)
        return 1

    comp = check_companions(plan)
    if comp:
        sys.stderr.write("watch: companion file(s) that cannot be derived, "
                         "nothing written:\n")
        for b in comp:
            sys.stderr.write("  %s\n" % b)
        return 1

    bad = verify(plan, opener=_TEST_OPENER)
    if bad:
        sys.stderr.write("watch: upstream URL check failed, nothing "
                         "written:\n")
        for b in bad:
            sys.stderr.write("  %s\n" % b)
        return 1

    allow = suggest_allow_lines(os_name, plan, args.allow_file)
    for item in sorted(plan, key=lambda p: p["path"]):
        if args.check:
            log("would write %s (from %s)" % (item["path"], item["source"]))
            continue
        with open(item["path"], "w", encoding="utf-8", newline="\n") as f:
            f.write(item["content"])
        log("wrote %s" % item["path"])
    if allow:
        log("suggested coverage.allow lines (apply in the anyvm repo):")
        for line in allow:
            log("  " + line)
        if args.allow_out and not args.check:
            with open(args.allow_out, "w", encoding="utf-8",
                      newline="\n") as f:
                f.write("\n".join(allow) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
