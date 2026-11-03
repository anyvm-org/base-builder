import http.server
import os
import random
import re
import sys
import tempfile
import threading
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build


class RangeHandler(http.server.BaseHTTPRequestHandler):
    """Serves server.payload, honouring Range unless server.script says
    otherwise for that exact Range header (each entry fires once), or
    server.fallback for any request the script does not name:
      "ignore"     answer 200 with the whole file, as a Cloudflare cache
                   miss in front of dlc.openindiana.org did
      "drop-half"  answer, but close after half the body
      "ignore-drop-half"  both
      "wrong-range" answer 206 with the right length from byte 0
      "error-<code>" answer that status with no body (524/525: Cloudflare
                   could not reach dlc.openindiana.org)
    server.fail_next holds status codes for the next requests the script
    does not name, the size probe ("bytes=0-0") excepted.
    server.reject_range answers 416 to every request carrying Range, and
    server.head_ok makes HEAD report the size (no Accept-Ranges).
    """
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_HEAD(self):
        if not self.server.head_ok:
            self.send_error(501)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.server.payload)))
        self.end_headers()

    def do_GET(self):
        srv = self.server
        rng = self.headers.get("Range")
        srv.requests.append(rng)
        data = srv.payload
        if rng and srv.reject_range:
            self.send_response(416)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        action = srv.script.pop(rng, None)
        if action is None and rng != "bytes=0-0" and srv.fail_next:
            action = "error-%d" % srv.fail_next.pop(0)
        action = action or srv.fallback or ""
        if action.startswith("error-"):
            self.send_response(int(action[len("error-"):]))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if rng and not action.startswith("ignore"):
            m = re.match(r"bytes=(\d+)-(\d*)$", rng)
            first = int(m.group(1))
            last = int(m.group(2)) if m.group(2) else len(data) - 1
            if action == "wrong-range":
                first, last = 0, last - first
            body = data[first:last + 1]
            self.send_response(206)
            self.send_header("Content-Range",
                             "bytes %d-%d/%d" % (first, last, len(data)))
        else:
            body = data
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if action.endswith("drop-half"):
            self.wfile.write(body[:len(body) // 2])
            self.close_connection = True
            return
        self.wfile.write(body)


class QuietServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # the client hanging up on an unwanted 200 body is expected
        pass


class DownloadCase(unittest.TestCase):
    SIZE = 8 * 1024 * 1024 + 12345     # >= DL_CHUNK_MIN, uneven chunks

    def setUp(self):
        self.srv = QuietServer(("127.0.0.1", 0), RangeHandler)
        self.srv.payload = random.Random(1).randbytes(self.SIZE)
        self.srv.script = {}
        self.srv.fallback = None
        self.srv.fail_next = []
        self.srv.reject_range = False
        self.srv.head_ok = False
        self.srv.requests = []
        t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        t.start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.url = "http://127.0.0.1:%d/img" % self.srv.server_address[1]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "img")
        for name, fake in (("log", lambda msg: None),
                           ("sleep", lambda secs: None)):
            owner = build.time if name == "sleep" else build
            self.addCleanup(setattr, owner, name, getattr(owner, name))
            setattr(owner, name, fake)

    def content(self):
        with open(self.path, "rb") as f:
            return f.read()


class TestRangeIgnored(DownloadCase):
    def test_chunk_answered_with_whole_file(self):
        # openindiana 202510: one of 8 chunks got a 200 + the whole file,
        # which was written at that chunk's offset -- the clean retry
        # could not shrink the file again (expected 929996550, got
        # 1394994826 = 4 chunks + the file).
        chunk = (self.SIZE + build.DL_THREADS - 1) // build.DL_THREADS
        start = 4 * chunk
        self.srv.script["bytes=%d-%d" % (start, start + chunk - 1)] = \
            "ignore"
        self.assertEqual(build.download(self.url, self.path), 0)
        self.assertEqual(len(self.content()), self.SIZE)
        self.assertTrue(self.content() == self.srv.payload)

    def test_resume_answered_with_whole_file(self):
        # a dropped stream resumed with "bytes=<got>-" that comes back as
        # 200 starts at byte 0, not at <got>
        half = self.SIZE // 2
        self.srv.script["bytes=0-"] = "drop-half"
        self.srv.script["bytes=%d-" % half] = "ignore"
        build._download_resume(self.url, self.path, self.SIZE)
        self.assertEqual(self.srv.requests, ["bytes=0-", "bytes=%d-" % half])
        self.assertEqual(len(self.content()), self.SIZE)
        self.assertTrue(self.content() == self.srv.payload)

    def test_resume_that_keeps_ignoring_range_gives_up(self):
        # every restart makes progress again, so the restart count -- not
        # the stall count -- is what has to end this
        self.srv.script["bytes=0-"] = "drop-half"
        self.srv.fallback = "ignore-drop-half"
        with self.assertRaises(IOError):
            build._download_resume(self.url, self.path, self.SIZE)
        self.assertLessEqual(len(self.srv.requests),
                             2 * build.DL_ATTEMPTS + 2)

    def test_chunk_answered_with_the_wrong_range(self):
        # a 206 of the right length for another range must not be
        # written as if it were this chunk -- right size, wrong bytes
        chunk = (self.SIZE + build.DL_THREADS - 1) // build.DL_THREADS
        start = 4 * chunk
        self.srv.script["bytes=%d-%d" % (start, start + chunk - 1)] = \
            "wrong-range"
        self.assertEqual(build.download(self.url, self.path), 0)
        self.assertTrue(self.content() == self.srv.payload)

    def test_resume_answered_with_the_wrong_range(self):
        half = self.SIZE // 2
        self.srv.script["bytes=0-"] = "drop-half"
        self.srv.script["bytes=%d-" % half] = "wrong-range"
        build._download_resume(self.url, self.path, self.SIZE)
        self.assertTrue(self.content() == self.srv.payload)

    def test_plain_ranged_download(self):
        self.assertEqual(build.download(self.url, self.path), 0)
        self.assertTrue(self.content() == self.srv.payload)


class TestServerErrors(DownloadCase):
    """openindiana 202604 / 202604-build: the probe found no usable Range,
    and the single stream died on its first 524 / 525."""

    def test_unranged_download_outlasts_server_errors(self):
        # probe answered 200: size known, no ranges
        self.srv.script["bytes=0-0"] = "ignore"
        self.srv.fail_next = [524, 525, 524]
        self.assertEqual(build.download(self.url, self.path), 0)
        self.assertTrue(self.content() == self.srv.payload)

    def test_unknown_size_download_outlasts_server_errors(self):
        # probe itself failed: size unknown, plain GET
        self.srv.script["bytes=0-0"] = "error-524"
        self.srv.fail_next = [525]
        self.assertEqual(build.download(self.url, self.path), 0)
        self.assertTrue(self.content() == self.srv.payload)

    def test_persistent_server_error_gives_up(self):
        self.srv.script["bytes=0-0"] = "error-524"
        self.srv.fallback = "error-524"
        with self.assertRaises(SystemExit):
            build.download(self.url, self.path)
        self.assertEqual(len(self.srv.requests),
                         1 + len(build.DL_RETRY_WAITS) + 1)

    def test_not_found_is_not_retried(self):
        self.srv.script["bytes=0-0"] = "error-404"
        self.srv.fallback = "error-404"
        with self.assertRaises(SystemExit):
            build.download(self.url, self.path)
        self.assertEqual(self.srv.requests, ["bytes=0-0", None])

    def test_range_rejected_but_plain_get_works(self):
        # size from HEAD, no usable Range: a plain GET, as before
        self.srv.reject_range = True
        self.srv.head_ok = True
        self.assertEqual(build.download(self.url, self.path), 0)
        self.assertTrue(self.content() == self.srv.payload)
        self.assertEqual(self.srv.requests, ["bytes=0-0", None])

    def test_short_plain_download_is_retried(self):
        # a stream that just ends early raises nothing in http.client
        self.srv.script["bytes=0-0"] = "ignore"
        self.srv.script[None] = "drop-half"
        self.assertEqual(build.download(self.url, self.path), 0)
        self.assertTrue(self.content() == self.srv.payload)

    def test_resume_not_found_is_not_retried(self):
        # every chunk 404s, so the resume fallback runs: one 404 there
        # is final, not three stalls times four attempts
        self.srv.script["bytes=0-0"] = "ok"
        self.srv.fallback = "error-404"
        with self.assertRaises(SystemExit):
            build.download(self.url, self.path)
        self.assertEqual(self.srv.requests.count("bytes=0-"), 1)

    def test_only_missing_urls_are_final(self):
        # 402 is how archive.netbsd.org throttles a stream mid-transfer
        for code in (402, 403, 408, 416, 429, 500, 524, 525, 404, 410):
            err = urllib.error.HTTPError(self.url, code, "x", {}, None)
            self.assertEqual(build._dl_transient(err),
                             code not in (404, 410), code)


if __name__ == "__main__":
    unittest.main()
