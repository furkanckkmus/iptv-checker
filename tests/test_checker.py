import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import iptv_checker as checker  # noqa: E402


def build_m3u(channels):
    lines = ["#EXTM3U"]
    for name, url in channels:
        lines.append(f'#EXTINF:-1 group-title="Test",{name}')
        lines.append(url)
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        base = f"http://127.0.0.1:{self.server.server_port}"
        path = self.path.split("?")[0]

        if path == "/good.m3u":
            body = build_m3u([(f"K{i}", base + "/ok.ts") for i in range(5)])
            self._send(200, body.encode(), "audio/x-mpegurl")
        elif path == "/weak.m3u":
            body = build_m3u(
                [("K1", base + "/ok.ts")]
                + [(f"K{i}", base + "/bad.ts") for i in range(2, 6)]
            )
            self._send(200, body.encode(), "audio/x-mpegurl")
        elif path == "/dead.m3u":
            body = build_m3u([(f"K{i}", base + "/bad.ts") for i in range(5)])
            self._send(200, body.encode(), "audio/x-mpegurl")
        elif path == "/empty.m3u":
            self._send(200, b"#EXTM3U\n", "audio/x-mpegurl")
        elif path == "/html":
            self._send(200, b"<html>login</html>", "text/html")
        elif path == "/hls.m3u8":
            body = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000000\nv.m3u8\n"
            self._send(200, body.encode(), "application/vnd.apple.mpegurl")
        elif path == "/v.m3u8":
            body = "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6.0,\nseg.ts\n"
            self._send(200, body.encode(), "application/vnd.apple.mpegurl")
        elif path == "/bad.m3u8":
            body = "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6.0,\nmissing.ts\n"
            self._send(200, body.encode(), "application/vnd.apple.mpegurl")
        elif path == "/seg.ts":
            self._send(200, b"\x47" + b"\x00" * 5000, "video/mp2t")
        elif path == "/ok.ts":
            self._send(200, b"\x47" + b"\x00" * 5000, "video/mp2t")
        elif path == "/500.m3u":
            body = build_m3u(
                [(f"K{i}", base + "/ok.ts") for i in range(1, 1001)]
            )
            self._send(200, body.encode(), "audio/x-mpegurl")
        else:
            self._send(404, b"not found", "text/plain")

    def _send(self, code, body, content_type):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class IPTVCheckerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_statuses(self):
        cases = [
            ("/good.m3u", checker.STATUS_HEALTHY),
            ("/weak.m3u", checker.STATUS_WEAK),
            ("/dead.m3u", checker.STATUS_NONE),
            ("/empty.m3u", checker.STATUS_BAD),
            ("/html", checker.STATUS_BAD),
            ("/hls.m3u8", checker.STATUS_HEALTHY),
            ("/bad.m3u8", checker.STATUS_NONE),
            ("/missing", checker.STATUS_BAD),
        ]
        for path, expected in cases:
            with self.subTest(path=path):
                result = checker.check_source(0, self.base + path)
                self.assertEqual(result.status, expected)

    def test_playlist_is_capped_at_500_lines(self):
        lines, code, error = checker.fetch_playlist(self.base + "/500.m3u")
        self.assertEqual(code, 200)
        self.assertEqual(error, "")
        self.assertEqual(len(lines), 500)

    def test_channel_selection(self):
        lines, _, _ = checker.fetch_playlist(self.base + "/500.m3u")
        channels = checker.parse_channels(lines)
        selected = checker.select_channels(channels)
        self.assertEqual(len(selected), 5)
        self.assertEqual(len({id(ch) for ch in selected}), 5)

    def test_credentials_are_preserved(self):
        url = "http://panel.example:8080/get.php?username=A%40B&password=x+y%40z"
        self.assertEqual(checker.extract_credentials(url), "A%40B:x+y%40z")
        self.assertIsNone(
            checker.extract_credentials(
                "http://panel.example:8080/get.php?username=only"
            )
        )

    def test_panel_normalization(self):
        url = "http://User:Pass@Example.COM:8080/get.php?username=a&password=b"
        self.assertEqual(checker.extract_panel(url), "http://example.com:8080/")

    def test_panel_records_and_deduplication(self):
        results = [
            checker.Result(
                index=0,
                url="http://p1.example:8080/get.php?username=user1&password=pass1",
                status=checker.STATUS_HEALTHY,
                playlist_opened=True,
            ),
            checker.Result(
                index=1,
                url="http://p1.example:8080/get.php?username=user2&password=pass2",
                status=checker.STATUS_WEAK,
                playlist_opened=True,
            ),
            checker.Result(
                index=2,
                url="http://p2.example:2095/get.php?username=user1&password=pass1",
                status=checker.STATUS_NONE,
                playlist_opened=True,
            ),
            checker.Result(
                index=3,
                url="http://bad.example:8080/get.php?username=user3&password=pass3",
                status=checker.STATUS_BAD,
                playlist_opened=False,
            ),
            checker.Result(
                index=4,
                url="http://empty.example:8080/get.php?username=user4&password=pass4",
                status=checker.STATUS_BAD,
                playlist_opened=True,
            ),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            panel_dir = Path(tmp) / "paneller"
            stats = checker.save_panel_records(results, panel_dir)

            panels = (panel_dir / "Paneller.txt").read_text(encoding="utf-8").splitlines()
            userpass = (panel_dir / "Userpass.txt").read_text(encoding="utf-8").splitlines()
            same = (panel_dir / "AyniUserpass.txt").read_text(encoding="utf-8")

            self.assertEqual(stats["panels_added"], 3)
            self.assertEqual(
                panels,
                [
                    "http://p1.example:8080/",
                    "http://p2.example:2095/",
                    "http://empty.example:8080/",
                ],
            )
            self.assertNotIn("http://bad.example:8080/", panels)
            self.assertEqual(
                userpass,
                ["user1:pass1", "user2:pass2", "user3:pass3", "user4:pass4"],
            )
            self.assertIn("user1:pass1", same)
            self.assertIn("http://p2.example:2095/", same)

            stats2 = checker.save_panel_records(results, panel_dir)
            self.assertEqual(stats2["panels_added"], 0)
            self.assertEqual(stats2["userpass_added"], 0)
            self.assertEqual(
                (panel_dir / "Paneller.txt").read_text(encoding="utf-8").splitlines(),
                panels,
            )

    def test_progress_frame(self):
        urls = [
            f"http://panel{i}.example:8080/get.php?username=u{i}&password=p{i}"
            for i in range(1, 21)
        ]
        frame = checker.build_progress_frame(
            total=20,
            completed={0, 1, 2, 3, 4},
            started={0, 1, 2, 3, 4, 5, 6, 7, 8},
            urls=urls,
            order=list(range(20)),
            parallelism=5,
            host_limit=4,
            elapsed=42.5,
        )

        self.assertIn("Tamamlanan   : 5", frame)
        self.assertIn("Kalan        : 15", frame)
        self.assertIn("Host limiti  : 4", frame)
        self.assertIn("#6, #7, #8, #9", frame)
        self.assertIn("Sıradaki 10:", frame)
        self.assertIn("[   10/20] panel10.example:8080", frame)
        self.assertIn("Hız (ort.)", frame)
        self.assertIn("█", frame)
        self.assertIn("░", frame)

    def test_speed_eta_switches_to_real_rate_after_90_percent(self):
        speed, eta_text, label = checker.compute_speed_eta(
            done=95,
            total=100,
            elapsed=100.0,
            p90_done=90,
            p90_time=90.0,
            now=100.0,
        )
        self.assertEqual(label, "90%+")
        self.assertAlmostEqual(speed, 0.5)
        self.assertEqual(eta_text, "00:00:10")

        speed, eta_text, label = checker.compute_speed_eta(
            done=95,
            total=100,
            elapsed=100.0,
            p90_done=None,
            p90_time=None,
            now=100.0,
        )
        self.assertEqual(label, "ort.")
        self.assertAlmostEqual(speed, 0.95)

    def test_run_folders_are_timestamped_and_unique(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "sonuclar"
            first = checker.make_run_folder(output_dir)
            second = checker.make_run_folder(output_dir)

            self.assertTrue(first.is_dir())
            self.assertRegex(first.name, r"^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}$")
            self.assertTrue(second.is_dir())
            self.assertTrue(second.name.endswith("_2"))

    def test_reports_use_timestamped_folder_with_plain_names(self):
        results = [
            checker.Result(
                index=0,
                url="http://p1.example:8080/get.php?username=user1&password=pass1",
                status=checker.STATUS_HEALTHY,
                playlist_opened=True,
            )
        ]
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "sonuclar"
            counts, run_dir = checker.write_reports(
                output_dir=output_dir,
                results=results,
                input_path=Path("input.txt"),
                total_lines=1,
                skipped=0,
                duplicates=0,
                elapsed=1.5,
            )

            self.assertEqual(counts[checker.STATUS_HEALTHY], 1)
            self.assertEqual(run_dir.parent, output_dir)
            for name in (
                "saglikli.txt",
                "zayif.txt",
                "yayin_yok.txt",
                "playlist_hatali.txt",
                "ozet.txt",
            ):
                self.assertTrue((run_dir / name).is_file(), name)
            self.assertIn("Tarama", (run_dir / "ozet.txt").read_text(encoding="utf-8"))

    def test_format_duration_and_host(self):
        self.assertEqual(checker.format_duration(0), "00:00:00")
        self.assertEqual(checker.format_duration(3725), "01:02:05")
        self.assertEqual(
            checker.display_host(
                "http://panel.example:8080/get.php?username=x&password=y"
            ),
            "panel.example:8080",
        )


if __name__ == "__main__":
    unittest.main()
