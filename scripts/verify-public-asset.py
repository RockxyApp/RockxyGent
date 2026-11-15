#!/usr/bin/env python3
"""Read-only gate: verify a public RockxyGent ZIP before publishing its appcast."""

import argparse
import base64
import binascii
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET


SPARKLE = "{http://www.andymatuschak.org/xml-namespaces/sparkle}"
REPOSITORY = "RockxyApp/RockxyGent"
ALLOWED_DOWNLOAD_HOSTS = {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}


def canonical(version, build):
    if not re.fullmatch(r"\d+\.\d+\.\d+", version) or not re.fullmatch(r"[1-9]\d*", build):
        raise ValueError("version/build must be numeric 0.1.0 and positive build forms")
    name = f"RockxyGent-{version}-{build}.zip"
    url = f"https://github.com/{REPOSITORY}/releases/download/v{version}/{name}"
    return name, url


def sha256_file(path):
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def validate_metadata(release, appcast, catalog, version, build, expected_sha, size):
    name, url = canonical(version, build)
    if (release.get("tag_name") != f"v{version}" or release.get("draft") is not False
            or release.get("prerelease") is not False):
        raise ValueError("public release tag/state does not match the reviewed version")
    assets = [asset for asset in release.get("assets", []) if asset.get("name") == name]
    if len(assets) != 1:
        raise ValueError("public release must contain exactly one canonical ZIP asset")
    asset = assets[0]
    if (asset.get("state") != "uploaded" or asset.get("browser_download_url") != url
            or asset.get("size") != size):
        raise ValueError("public release asset URL, state or size differs from local ZIP")
    if asset.get("digest") not in (None, "sha256:" + expected_sha):
        raise ValueError("GitHub asset digest differs from local ZIP")

    root = ET.parse(appcast).getroot()
    matches = [item for item in root.findall("./channel/item")
               if item.findtext(SPARKLE + "version") == build
               and item.findtext(SPARKLE + "shortVersionString") == version]
    if len(matches) != 1:
        raise ValueError("appcast must contain exactly one matching version/build")
    enclosure = matches[0].find("enclosure")
    if enclosure is None or enclosure.get("url") != url or enclosure.get("length") != str(size):
        raise ValueError("appcast enclosure URL or byte length differs from public ZIP")
    try:
        signature = base64.b64decode(enclosure.get(SPARKLE + "edSignature", ""), validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("appcast Sparkle signature is invalid") from None
    if len(signature) != 64:
        raise ValueError("appcast Sparkle signature must decode to 64 bytes")

    rows = json.loads(catalog.read_text())
    matches = [row for row in rows if row.get("product_code") == "rockxygent"
               and row.get("version") == version and row.get("build") == build]
    if len(matches) != 1:
        raise ValueError("catalog must contain exactly one matching RockxyGent row")
    row = matches[0]
    if (row.get("download_url") != url or row.get("checksum") != "sha256:" + expected_sha
            or row.get("sparkle_signature") != enclosure.get(SPARKLE + "edSignature")):
        raise ValueError("catalog URL, checksum or signature differs from appcast/public ZIP")
    return url


def fetch_public_release(version):
    request = Request(f"https://api.github.com/repos/{REPOSITORY}/releases/tags/v{version}",
                      headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                               "User-Agent": "RockxyGent-release-verifier"})
    with urlopen(request, timeout=20) as response:
        return json.load(response)


def hash_public_download(url):
    request = Request(url, headers={"User-Agent": "RockxyGent-release-verifier"})
    with urlopen(request, timeout=30) as response:
        final = urlsplit(response.geturl())
        if final.scheme != "https" or final.hostname not in ALLOWED_DOWNLOAD_HOSTS:
            raise ValueError("public asset redirected outside GitHub download hosts")
        digest = hashlib.sha256()
        size = 0
        for chunk in iter(lambda: response.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
        return digest.hexdigest(), size


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--appcast", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--build", required=True)
    parser.add_argument("--expected-sha256", required=True, help="archive_sha256 from the private release record")
    args = parser.parse_args()
    try:
        if not re.fullmatch(r"[0-9a-f]{64}", args.expected_sha256):
            raise ValueError("expected SHA-256 must be 64 lowercase hexadecimal characters")
        name, _ = canonical(args.version, args.build)
        if args.archive.name != name:
            raise ValueError("local archive name differs from canonical release asset")
        local_sha, local_size = sha256_file(args.archive)
        if local_sha != args.expected_sha256:
            raise ValueError("local ZIP differs from private release-record SHA-256")
        release = fetch_public_release(args.version)
        url = validate_metadata(release, args.appcast, args.catalog, args.version, args.build,
                                local_sha, local_size)
        remote_sha, remote_size = hash_public_download(url)
        if (remote_sha, remote_size) != (local_sha, local_size):
            raise ValueError("publicly downloaded ZIP differs from reviewed local ZIP")
    except (ValueError, OSError, ET.ParseError, json.JSONDecodeError, HTTPError, URLError) as error:
        print(f"PUBLIC_ASSET_NOT_VERIFIED: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(f"PUBLIC_ASSET_VERIFIED version={args.version} build={args.build} sha256={local_sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
