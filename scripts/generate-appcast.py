#!/usr/bin/env python3
"""Generate a Sparkle feed entry only from a real signed release archive.

This is preparation for a future update-enabled RockxyGent build. Source feed
metadata alone does not prove an integrated update path. The release owner must
verify the archive, signature, feed URL and in-app update before publication.
The reviewed --released-at value must be the first public release time and
match the backend release catalog; never regenerate it from the current time.
"""

import argparse
import base64
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import tempfile
from urllib.parse import urlparse
import xml.etree.ElementTree as ET
import zipfile

SPARKLE_NS = "http://www.andymatuschak.org/xml-namespaces/sparkle"
FEED_URL = "https://raw.githubusercontent.com/RockxyApp/RockxyGent/main/appcast.xml"
ET.register_namespace("sparkle", SPARKLE_NS)


def create_outputs(appcast_path: Path, appcast_bytes: bytes,
                   catalog_path: Path, catalog_bytes: bytes):
    """Create both reviewed outputs without replacing a pre-existing path."""
    staged = []
    published = []
    try:
        for destination, content in ((catalog_path, catalog_bytes), (appcast_path, appcast_bytes)):
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".rockxygent-release-",
                                             delete=False) as temporary:
                staged.append((Path(temporary.name), destination))
                temporary.write(content)
        for temporary, destination in staged:
            os.link(temporary, destination)
            published.append((temporary, destination))
    except BaseException:
        for temporary, destination in published:
            try:
                if destination.lstat().st_ino == temporary.stat().st_ino:
                    destination.unlink()
            except FileNotFoundError:
                pass
        raise
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)


def rockxygent_release_identity(version: str, build: str) -> bool:
    return (re.fullmatch(r"\d+\.\d+\.\d+", version) is not None and
            re.fullmatch(r"[1-9]\d*", build) is not None)


def archive_contains_exact_app(archive_path: Path, app_path: Path) -> bool:
    """Compare the archive's app tree with the reviewed bundle, without extracting it."""
    root = "RockxyGent.app"
    local = {}
    for path in app_path.rglob("*"):
        relative = path.relative_to(app_path).as_posix()
        local[f"{root}/{relative}"] = path
    archived = {}
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for entry in archive.infolist():
                name = entry.filename.rstrip("/")
                if name == root:
                    continue
                if not name.startswith(root + "/") or "/../" in name or name.endswith("/.."):
                    return False
                # ditto stores resource-fork metadata in AppleDouble companions.
                if name.rsplit("/", 1)[-1].startswith("._"):
                    continue
                if name in archived:
                    return False
                archived[name] = entry
            if set(local) != set(archived):
                return False
            for name, path in local.items():
                entry = archived[name]
                host_mode = path.lstat().st_mode
                archive_mode = entry.external_attr >> 16
                if stat.S_ISDIR(host_mode):
                    if not entry.is_dir() or not stat.S_ISDIR(archive_mode):
                        return False
                    continue
                if stat.S_ISLNK(host_mode):
                    if not stat.S_ISLNK(archive_mode):
                        return False
                    expected = os.fsencode(os.readlink(path))
                    if archive.read(entry) != expected:
                        return False
                    continue
                if not stat.S_ISREG(host_mode) or not stat.S_ISREG(archive_mode):
                    return False
                if entry.file_size != path.stat().st_size:
                    return False
                with path.open("rb") as source, archive.open(entry) as packed:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        if packed.read(len(chunk)) != chunk:
                            return False
                    if packed.read(1):
                        return False
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError):
        return False
    return bool(local)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--sparkle-bin", type=Path, required=True)
    parser.add_argument("--sparkle-account", default="rockxygent")
    parser.add_argument("--version", required=True)
    parser.add_argument("--build", required=True)
    parser.add_argument("--download-url", required=True)
    parser.add_argument("--ed-signature", required=True,
                        help="Sparkle EdDSA signature generated by Sparkle's signing tool")
    parser.add_argument("--minimum-system-version", required=True)
    parser.add_argument("--released-at", required=True,
                        help="immutable first-publication UTC timestamp, YYYY-MM-DDTHH:MM:SSZ")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--catalog-output", type=Path, required=True,
                        help="create-only backend release catalog JSON from the same verified archive")
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--previous-catalog", type=Path,
                        help="required with --previous so old eligible releases remain available")
    args = parser.parse_args()

    if bool(args.previous) != bool(args.previous_catalog):
        parser.error("--previous and --previous-catalog must be provided together")

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", args.released_at):
        parser.error("released-at must be an exact UTC timestamp ending in Z")
    try:
        released_at = datetime.strptime(args.released_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        parser.error("released-at is not a valid UTC timestamp")

    if not re.fullmatch(r"\d+\.\d+\.\d+", args.version):
        parser.error("version must be major.minor.patch")
    if not re.fullmatch(r"[1-9]\d*", args.build):
        parser.error("build must be a positive integer")
    if not re.fullmatch(r"\d+\.\d+", args.minimum_system_version):
        parser.error("minimum system version must be major.minor")
    if not args.archive.is_file() or args.archive.stat().st_size == 0:
        parser.error("archive must be a nonempty regular file")
    if args.archive.suffix != ".zip":
        parser.error("RockxyGent release archive must be a ZIP")
    if args.archive.name != f"RockxyGent-{args.version}-{args.build}.zip":
        parser.error("archive filename must match product, version and build")
    url = urlparse(args.download_url)
    if (url.scheme != "https" or url.netloc != "github.com" or
            url.path != f"/RockxyApp/RockxyGent/releases/download/v{args.version}/{args.archive.name}" or
            url.query or url.fragment):
        parser.error("download URL must point to the exact versioned archive in the public release")
    try:
        signature = base64.b64decode(args.ed_signature, validate=True)
    except ValueError:
        parser.error("EdDSA signature must be base64")
    if len(signature) != 64:
        parser.error("EdDSA signature must decode to 64 bytes")
    if args.output.resolve() == args.catalog_output.resolve():
        parser.error("appcast and catalog outputs must be different paths")
    if os.path.lexists(args.output) or os.path.lexists(args.catalog_output):
        parser.error("output already exists; review existing feed and catalog before replacing either")

    info_path = args.app / "Contents/Info.plist"
    if not info_path.is_file():
        parser.error("final app bundle is missing Info.plist")
    with info_path.open("rb") as source:
        info = plistlib.load(source)
    public_key = info.get("SUPublicEDKey")
    try:
        key_bytes = base64.b64decode(public_key, validate=True)
    except (ValueError, TypeError):
        parser.error("app lacks a valid Sparkle public key")
    if len(key_bytes) != 32:
        parser.error("app lacks a valid Sparkle public key")
    if (info.get("CFBundleIdentifier") != "com.amunx.RockxyGent" or
            info.get("CFBundleShortVersionString") != args.version or
            str(info.get("CFBundleVersion")) != args.build or
            info.get("RockxyGentReleasedAt") != args.released_at or
            info.get("SUFeedURL") != FEED_URL or
            info.get("LSMinimumSystemVersion") != args.minimum_system_version):
        parser.error("app identity, version, release date, feed or minimum macOS differs")
    if not archive_contains_exact_app(args.archive, args.app):
        parser.error("release archive app differs from the reviewed app bundle")
    signer = args.sparkle_bin / "sign_update"
    key_reader = args.sparkle_bin / "generate_keys"
    if not signer.is_file() or not key_reader.is_file():
        parser.error("Sparkle tools not found")
    try:
        key = subprocess.run([str(key_reader), "--account", args.sparkle_account, "-p"],
                             check=True, capture_output=True, text=True).stdout.strip()
        if key != public_key:
            parser.error("app public key differs from signing account")
        subprocess.run([str(signer), "--account", args.sparkle_account, "--verify",
                        str(args.archive), args.ed_signature], check=True,
                       capture_output=True, text=True)
    except subprocess.CalledProcessError:
        parser.error("Sparkle signature verification failed")

    prior_catalog = []
    if args.previous:
        if not args.previous.is_file():
            parser.error("previous feed is missing")
        channel = ET.parse(args.previous).getroot()
        feed = channel.find("channel")
        if channel.tag != "rss" or feed is None or feed.findtext("link") != FEED_URL:
            parser.error("previous feed has unexpected identity")
        prior = feed.findall("item")
        if any(i.findtext(f"{{{SPARKLE_NS}}}shortVersionString") == args.version for i in prior):
            parser.error("version already in feed")
        try:
            builds = [int(i.findtext(f"{{{SPARKLE_NS}}}version")) for i in prior]
        except (TypeError, ValueError):
            parser.error("previous feed contains a malformed build")
        if builds and int(args.build) <= max(builds):
            parser.error("build does not advance feed")
        for previous_item in prior:
            raw_date = previous_item.findtext("pubDate")
            if raw_date is None:
                parser.error("previous feed item lacks an immutable pubDate")
            try:
                previous_date = parsedate_to_datetime(raw_date)
            except (TypeError, ValueError, IndexError):
                parser.error("previous feed item has invalid pubDate")
            if previous_date.tzinfo is None or released_at <= previous_date.astimezone(timezone.utc):
                parser.error("released-at must advance every previous feed pubDate")
        if not args.previous_catalog.is_file():
            parser.error("previous release catalog is missing")
        try:
            prior_catalog = json.loads(args.previous_catalog.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            parser.error("previous release catalog is not valid JSON")
        if not isinstance(prior_catalog, list) or len(prior_catalog) != len(prior):
            parser.error("previous release catalog must have one row per appcast item")
        indexed_catalog = {}
        for row in prior_catalog:
            if not isinstance(row, dict) or row.get("product_code") != "rockxygent" or \
                    row.get("platform") != "macos" or row.get("channel") != "stable":
                parser.error("previous catalog contains a non-RockxyGent stable release")
            key = (row.get("version"), row.get("build"))
            if key in indexed_catalog or not all(isinstance(part, str) for part in key):
                parser.error("previous catalog has duplicate or malformed release identity")
            if not rockxygent_release_identity(*key):
                parser.error("previous catalog has malformed release version or build")
            checksum = row.get("checksum")
            if not isinstance(checksum, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", checksum) is None:
                parser.error("previous catalog has an invalid SHA-256 checksum")
            indexed_catalog[key] = row
        matched_keys = set()
        for previous_item in prior:
            key = (previous_item.findtext(f"{{{SPARKLE_NS}}}shortVersionString"),
                   previous_item.findtext(f"{{{SPARKLE_NS}}}version"))
            row = indexed_catalog.get(key)
            if key in matched_keys:
                parser.error("previous appcast has duplicate release identity")
            matched_keys.add(key)
            enclosure = previous_item.find("enclosure")
            raw_date = previous_item.findtext("pubDate")
            parsed_date = parsedate_to_datetime(raw_date).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if row is None or enclosure is None or \
                    row.get("release_date") != parsed_date or \
                    row.get("download_url") != enclosure.get("url") or \
                    row.get("sparkle_signature") != enclosure.get(f"{{{SPARKLE_NS}}}edSignature") or \
                    row.get("minimum_system_version") != previous_item.findtext(f"{{{SPARKLE_NS}}}minimumSystemVersion") or \
                    row.get("release_notes_url") != previous_item.findtext(f"{{{SPARKLE_NS}}}fullReleaseNotesLink"):
                parser.error("previous catalog and appcast release metadata differ")
        if matched_keys != set(indexed_catalog):
            parser.error("previous catalog contains a release absent from appcast")
    else:
        channel = ET.Element("rss", {"version": "2.0"})
        feed = ET.SubElement(channel, "channel")
        ET.SubElement(feed, "title").text = "RockxyGent"
        ET.SubElement(feed, "link").text = FEED_URL
        ET.SubElement(feed, "description").text = "RockxyGent release channel"
        ET.SubElement(feed, "language").text = "en"
    item = ET.Element("item")
    ET.SubElement(item, "title").text = f"RockxyGent {args.version}"
    ET.SubElement(item, "pubDate").text = format_datetime(released_at, usegmt=True)
    ET.SubElement(item, f"{{{SPARKLE_NS}}}version").text = args.build
    ET.SubElement(item, f"{{{SPARKLE_NS}}}shortVersionString").text = args.version
    ET.SubElement(item, f"{{{SPARKLE_NS}}}fullReleaseNotesLink").text = (
        f"https://github.com/RockxyApp/RockxyGent/releases/tag/v{args.version}")
    ET.SubElement(item, "enclosure", {
        "url": args.download_url,
        "length": str(args.archive.stat().st_size),
        "type": "application/zip",
        f"{{{SPARKLE_NS}}}edSignature": args.ed_signature,
    })
    ET.SubElement(item, f"{{{SPARKLE_NS}}}minimumSystemVersion").text = args.minimum_system_version
    feed.insert(4, item)
    digest = hashlib.sha256()
    with args.archive.open("rb") as archive:
        for chunk in iter(lambda: archive.read(1024 * 1024), b""):
            digest.update(chunk)
    archive_sha256 = digest.hexdigest()
    notes_url = f"https://github.com/RockxyApp/RockxyGent/releases/tag/v{args.version}"
    new_record = {
        "product_code": "rockxygent", "platform": "macos", "channel": "stable",
        "version": args.version, "build": args.build, "release_date": args.released_at,
        "download_url": args.download_url, "checksum": f"sha256:{archive_sha256}",
        "sparkle_signature": args.ed_signature, "release_notes_url": notes_url,
        "minimum_system_version": args.minimum_system_version,
    }
    catalog = sorted([*prior_catalog, new_record],
                     key=lambda row: (row["release_date"], int(row["build"])))
    appcast_bytes = ET.tostring(channel, encoding="utf-8", xml_declaration=True)
    catalog_bytes = (json.dumps(catalog, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        create_outputs(args.output, appcast_bytes, args.catalog_output, catalog_bytes)
    except OSError as error:
        parser.error(f"could not create release outputs: {error.strerror or error}")
    print("archive_sha256=" + archive_sha256)
    print("appcast=" + str(args.output))
    print("release_catalog=" + str(args.catalog_output))


if __name__ == "__main__":
    main()
