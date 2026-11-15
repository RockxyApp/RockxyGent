"""Synthetic generator contract tests; these do not prove a real Sparkle signature."""

import base64
import hashlib
import importlib.util
import json
import os
import plistlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

SCRIPT = Path(__file__).with_name("generate-appcast.py")
SPARKLE = "http://www.andymatuschak.org/xml-namespaces/sparkle"
spec = importlib.util.spec_from_file_location("rockxygent_appcast_generator", SCRIPT)
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)


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
        self.catalog = self.root / "release-catalog.json"

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
            "--output", str(self.output), "--catalog-output", str(self.catalog), *extra,
        ], capture_output=True, text=True)

    def write_previous(self, date="Thu, 19 Nov 2026 12:30:00 GMT"):
        previous_feed = self.root / "previous.xml"
        previous_catalog = self.root / "previous-catalog.json"
        old_url = "https://github.com/RockxyApp/RockxyGent/releases/download/v0.0.9/RockxyGent-0.0.9-1.zip"
        old_notes = "https://github.com/RockxyApp/RockxyGent/releases/tag/v0.0.9"
        old_item = ET.Element("item")
        ET.SubElement(old_item, "pubDate").text = date
        ET.SubElement(old_item, f"{{{SPARKLE}}}version").text = "1"
        ET.SubElement(old_item, f"{{{SPARKLE}}}shortVersionString").text = "0.0.9"
        ET.SubElement(old_item, f"{{{SPARKLE}}}fullReleaseNotesLink").text = old_notes
        ET.SubElement(old_item, f"{{{SPARKLE}}}minimumSystemVersion").text = "14.0"
        ET.SubElement(old_item, "enclosure", {"url": old_url, f"{{{SPARKLE}}}edSignature": self.signature})
        root = ET.Element("rss", {"version": "2.0"})
        channel = ET.SubElement(root, "channel")
        ET.SubElement(channel, "link").text = self.info["SUFeedURL"]
        channel.append(old_item)
        ET.ElementTree(root).write(previous_feed, encoding="utf-8", xml_declaration=True)
        old_date = "2026-11-19T12:30:00Z" if "19 Nov" in date else "2026-11-21T12:30:00Z"
        old_record = {"product_code": "rockxygent", "platform": "macos", "channel": "stable",
                      "version": "0.0.9", "build": "1", "release_date": old_date,
                      "download_url": old_url, "checksum": "sha256:" + "a" * 64,
                      "sparkle_signature": self.signature, "release_notes_url": old_notes,
                      "minimum_system_version": "14.0"}
        previous_catalog.write_text(json.dumps([old_record]))
        return previous_feed, previous_catalog

    def test_generates_structural_feed_from_synthetic_fixture(self):
        result = self.run_generator()
        self.assertEqual(result.returncode, 0, result.stderr)
        root = ET.parse(self.output).getroot()
        self.assertEqual(root.findtext("./channel/link"), self.info["SUFeedURL"])
        item = root.find("./channel/item")
        self.assertEqual(item.findtext(f"{{{SPARKLE}}}version"), "2")
        self.assertEqual(item.findtext("pubDate"), "Fri, 20 Nov 2026 12:30:00 GMT")
        self.assertEqual(item.find("enclosure").get("length"), str(self.archive.stat().st_size))
        catalog = json.loads(self.catalog.read_text())
        self.assertEqual(len(catalog), 1)
        record = catalog[0]
        self.assertEqual(record["product_code"], "rockxygent")
        self.assertEqual(record["platform"], "macos")
        self.assertEqual(record["channel"], "stable")
        self.assertEqual(record["version"], "0.1.0")
        self.assertEqual(record["build"], item.findtext(f"{{{SPARKLE}}}version"))
        self.assertEqual(record["release_date"], self.info["RockxyGentReleasedAt"])
        self.assertEqual(record["download_url"], item.find("enclosure").get("url"))
        self.assertEqual(record["sparkle_signature"], item.find("enclosure").get(f"{{{SPARKLE}}}edSignature"))
        self.assertEqual(record["minimum_system_version"], item.findtext(f"{{{SPARKLE}}}minimumSystemVersion"))
        self.assertEqual(record["checksum"], "sha256:" + hashlib.sha256(self.archive.read_bytes()).hexdigest())

    def test_rejects_app_without_embedded_feed(self):
        del self.info["SUFeedURL"]
        self.write_info()
        result = self.run_generator()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.catalog.exists())

    def test_rejects_app_with_different_embedded_release_date(self):
        self.info["RockxyGentReleasedAt"] = "2026-11-20T12:30:01Z"
        self.write_info()
        result = self.run_generator()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.catalog.exists())

    def test_rejects_existing_output(self):
        self.output.write_text("keep")
        result = self.run_generator()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.output.read_text(), "keep")
        self.assertFalse(self.catalog.exists())

    def test_rejects_existing_catalog_without_writing_feed(self):
        self.catalog.write_text("keep")
        result = self.run_generator()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.catalog.read_text(), "keep")
        self.assertFalse(self.output.exists())

    def test_rejects_same_output_path_and_broken_symlink(self):
        result = self.run_generator("--catalog-output", str(self.output))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())
        self.output.symlink_to(self.root / "missing-feed.xml")
        result = self.run_generator()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.output.is_symlink())
        self.assertFalse(self.catalog.exists())

    def test_second_output_failure_rolls_back_first_owned_output(self):
        original_link = os.link
        calls = 0

        def fail_second(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("synthetic second-output failure")
            return original_link(source, destination)

        with patch.object(generator.os, "link", side_effect=fail_second):
            with self.assertRaises(OSError):
                generator.create_outputs(self.output, b"feed", self.catalog, b"catalog")
        self.assertFalse(self.output.exists())
        self.assertFalse(self.catalog.exists())
        self.assertEqual(list(self.root.glob(".rockxygent-release-*")), [])

    def test_rejects_invalid_or_non_utc_release_date(self):
        for value in ("2026-11-20T12:30:00+00:00", "2026-02-30T12:30:00Z"):
            with self.subTest(value=value):
                result = self.run_generator("--released-at", value)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.output.exists())
                self.assertFalse(self.catalog.exists())

    def test_previous_feed_requires_earlier_immutable_date(self):
        previous, old_catalog = self.write_previous("Sat, 21 Nov 2026 12:30:00 GMT")
        result = self.run_generator("--previous", str(previous), "--previous-catalog", str(old_catalog))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("advance every previous", result.stderr)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.catalog.exists())
        previous, old_catalog = self.write_previous()
        result = self.run_generator("--previous", str(previous), "--previous-catalog", str(old_catalog))
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = json.loads(self.catalog.read_text())
        self.assertEqual([row["version"] for row in rows], ["0.0.9", "0.1.0"])
        self.assertEqual(rows[0]["release_date"], "2026-11-19T12:30:00Z")
        self.assertEqual(len(ET.parse(self.output).getroot().findall("./channel/item")), 2)

    def test_previous_feed_and_catalog_must_be_paired_and_match(self):
        previous, old_catalog = self.write_previous()
        self.assertNotEqual(self.run_generator("--previous", str(previous)).returncode, 0)
        self.assertNotEqual(self.run_generator("--previous-catalog", str(old_catalog)).returncode, 0)
        for field, value in (("release_date", "2026-11-18T12:30:00Z"),
                             ("download_url", "https://github.com/RockxyApp/other/releases/download/v0.0.9/other.zip"),
                             ("sparkle_signature", base64.b64encode(b"x" * 64).decode()),
                             ("checksum", "sha256:bad")):
            with self.subTest(field=field):
                previous, old_catalog = self.write_previous()
                rows = json.loads(old_catalog.read_text())
                rows[0][field] = value
                old_catalog.write_text(json.dumps(rows))
                result = self.run_generator("--previous", str(previous), "--previous-catalog", str(old_catalog))
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.output.exists())
                self.assertFalse(self.catalog.exists())
        previous, old_catalog = self.write_previous()
        rows = json.loads(old_catalog.read_text())
        old_catalog.write_text(json.dumps(rows + rows))
        result = self.run_generator("--previous", str(previous), "--previous-catalog", str(old_catalog))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.catalog.exists())

    def test_previous_feed_rejects_malformed_or_duplicate_build_identity(self):
        previous, old_catalog = self.write_previous()
        feed = ET.parse(previous)
        feed.find("./channel/item").find(f"{{{SPARKLE}}}version").text = "bad"
        feed.write(previous, encoding="utf-8", xml_declaration=True)
        result = self.run_generator("--previous", str(previous), "--previous-catalog", str(old_catalog))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("malformed build", result.stderr)
        self.assertFalse(self.output.exists())
        previous, old_catalog = self.write_previous()
        feed = ET.parse(previous)
        feed.find("./channel").append(ET.fromstring(ET.tostring(feed.find("./channel/item"))))
        feed.write(previous, encoding="utf-8", xml_declaration=True)
        rows = json.loads(old_catalog.read_text())
        extra = dict(rows[0], version="0.0.8")
        old_catalog.write_text(json.dumps([*rows, extra]))
        result = self.run_generator("--previous", str(previous), "--previous-catalog", str(old_catalog))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("duplicate release identity", result.stderr)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
