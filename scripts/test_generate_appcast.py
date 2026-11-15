"""Synthetic generator contract tests; these do not prove a real Sparkle signature."""

import base64
import plistlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

SCRIPT = Path(__file__).with_name("generate-appcast.py")
SPARKLE = "http://www.andymatuschak.org/xml-namespaces/sparkle"


class AppcastPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / "RockxyGent-0.1.0-2.zip"
        self.archive.write_bytes(b"synthetic archive")
        self.app = self.root / "RockxyGent.app"
        (self.app / "Contents").mkdir(parents=True)
        self.key = base64.b64encode(b"k" * 32).decode()
        self.signature = base64.b64encode(b"s" * 64).decode()
        self.info = {
            "CFBundleIdentifier": "com.amunx.RockxyGent",
            "CFBundleShortVersionString": "0.1.0",
            "CFBundleVersion": "2",
            "RockxyGentReleasedAt": "2026-11-20T12:30:00Z",
            "LSMinimumSystemVersion": "14.0",
            "SUFeedURL": "https://raw.githubusercontent.com/RockxyApp/RockxyGent/main/appcast.xml",
            "SUPublicEDKey": self.key,
        }
        self.write_info()
        self.tools = self.root / "tools"
        self.tools.mkdir()
        for name, output in (("generate_keys", self.key), ("sign_update", "")):
            tool = self.tools / name
            tool.write_text("#!/bin/sh\nprintf '%s\\n' '" + output + "'\n")
            tool.chmod(0o755)
        self.output = self.root / "appcast.xml"

    def write_info(self):
        with (self.app / "Contents/Info.plist").open("wb") as destination:
            plistlib.dump(self.info, destination)

    def run_generator(self, *extra):
        return subprocess.run([
            sys.executable, str(SCRIPT), "--archive", str(self.archive),
            "--app", str(self.app), "--sparkle-bin", str(self.tools),
            "--version", "0.1.0", "--build", "2",
            "--download-url", "https://github.com/RockxyApp/RockxyGent/releases/download/v0.1.0/RockxyGent-0.1.0-2.zip",
            "--ed-signature", self.signature, "--minimum-system-version", "14.0",
            "--released-at", "2026-11-20T12:30:00Z",
            "--output", str(self.output), *extra,
        ], capture_output=True, text=True)

    def test_generates_structural_feed_from_synthetic_fixture(self):
        result = self.run_generator()
        self.assertEqual(result.returncode, 0, result.stderr)
        root = ET.parse(self.output).getroot()
        self.assertEqual(root.findtext("./channel/link"), self.info["SUFeedURL"])
        item = root.find("./channel/item")
        self.assertEqual(item.findtext(f"{{{SPARKLE}}}version"), "2")
        self.assertEqual(item.findtext("pubDate"), "Fri, 20 Nov 2026 12:30:00 GMT")
        self.assertEqual(item.find("enclosure").get("length"), str(self.archive.stat().st_size))

    def test_rejects_app_without_embedded_feed(self):
        del self.info["SUFeedURL"]
        self.write_info()
        result = self.run_generator()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())

    def test_rejects_app_with_different_embedded_release_date(self):
        self.info["RockxyGentReleasedAt"] = "2026-11-20T12:30:01Z"
        self.write_info()
        result = self.run_generator()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())

    def test_rejects_existing_output(self):
        self.output.write_text("keep")
        result = self.run_generator()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.output.read_text(), "keep")

    def test_rejects_invalid_or_non_utc_release_date(self):
        for value in ("2026-11-20T12:30:00+00:00", "2026-02-30T12:30:00Z"):
            with self.subTest(value=value):
                result = self.run_generator("--released-at", value)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.output.exists())

    def test_previous_feed_requires_earlier_immutable_date(self):
        previous = self.root / "previous.xml"
        previous.write_text("""<rss version="2.0" xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle"><channel>
        <link>https://raw.githubusercontent.com/RockxyApp/RockxyGent/main/appcast.xml</link>
        <item><pubDate>Sat, 21 Nov 2026 12:30:00 GMT</pubDate><sparkle:version>1</sparkle:version>
        <sparkle:shortVersionString>0.0.9</sparkle:shortVersionString></item>
        </channel></rss>""")
        result = self.run_generator("--previous", str(previous))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("advance every previous", result.stderr)
        self.assertFalse(self.output.exists())
        previous.write_text(previous.read_text().replace("21 Nov", "19 Nov"))
        result = self.run_generator("--previous", str(previous))
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
