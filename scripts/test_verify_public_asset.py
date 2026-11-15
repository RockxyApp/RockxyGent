import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import xml.etree.ElementTree as ET


SCRIPT = Path(__file__).with_name("verify-public-asset.py")
spec = importlib.util.spec_from_file_location("verify_public_asset", SCRIPT)
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


class PublicAssetVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.name, self.url = verifier.canonical("0.1.0", "2")
        self.archive = self.root / self.name
        self.archive.write_bytes(b"synthetic RockxyGent ZIP fixture")
        self.sha = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.signature = base64.b64encode(b"s" * 64).decode()
        self.release = {"tag_name": "v0.1.0", "draft": False, "prerelease": False,
                        "assets": [{"name": self.name, "state": "uploaded", "size": self.archive.stat().st_size,
                                    "browser_download_url": self.url, "digest": "sha256:" + self.sha}]}
        root = ET.Element("rss")
        channel = ET.SubElement(root, "channel")
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, verifier.SPARKLE + "version").text = "2"
        ET.SubElement(item, verifier.SPARKLE + "shortVersionString").text = "0.1.0"
        ET.SubElement(item, "enclosure", {"url": self.url, "length": str(self.archive.stat().st_size),
                                           verifier.SPARKLE + "edSignature": self.signature})
        self.appcast = self.root / "appcast.xml"
        ET.ElementTree(root).write(self.appcast)
        self.catalog = self.root / "catalog.json"
        self.catalog.write_text(json.dumps([{"product_code": "rockxygent", "version": "0.1.0", "build": "2",
                                             "download_url": self.url, "checksum": "sha256:" + self.sha,
                                             "sparkle_signature": self.signature}]))

    def test_matching_public_asset_feed_and_catalog_pass(self):
        self.assertEqual(verifier.validate_metadata(self.release, self.appcast, self.catalog,
                                                    "0.1.0", "2", self.sha, self.archive.stat().st_size), self.url)

    def test_draft_wrong_asset_or_digest_is_refused(self):
        for change in ({"draft": True}, {"assets": []},
                       {"assets": [{**self.release["assets"][0], "digest": "sha256:" + "0" * 64}]}):
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    verifier.validate_metadata({**self.release, **change}, self.appcast, self.catalog,
                                               "0.1.0", "2", self.sha, self.archive.stat().st_size)

    def test_feed_and_catalog_mismatch_are_refused(self):
        self.catalog.write_text("[]")
        with self.assertRaises(ValueError):
            verifier.validate_metadata(self.release, self.appcast, self.catalog,
                                       "0.1.0", "2", self.sha, self.archive.stat().st_size)

    def test_main_refuses_changed_public_bytes(self):
        arguments = ["verify-public-asset.py", "--archive", str(self.archive), "--appcast", str(self.appcast),
                     "--catalog", str(self.catalog), "--version", "0.1.0", "--build", "2",
                     "--expected-sha256", self.sha]
        with mock.patch.object(sys, "argv", arguments), mock.patch.object(verifier, "fetch_public_release", return_value=self.release), \
             mock.patch.object(verifier, "hash_public_download", return_value=("0" * 64, self.archive.stat().st_size)):
            self.assertEqual(verifier.main(), 1)

    def test_main_accepts_exact_public_bytes(self):
        arguments = ["verify-public-asset.py", "--archive", str(self.archive), "--appcast", str(self.appcast),
                     "--catalog", str(self.catalog), "--version", "0.1.0", "--build", "2",
                     "--expected-sha256", self.sha]
        with mock.patch.object(sys, "argv", arguments), mock.patch.object(verifier, "fetch_public_release", return_value=self.release), \
             mock.patch.object(verifier, "hash_public_download", return_value=(self.sha, self.archive.stat().st_size)):
            self.assertEqual(verifier.main(), 0)


if __name__ == "__main__":
    unittest.main()
