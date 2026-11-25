"""Bounded loader/cache for official passive-tree atlas images."""
from __future__ import annotations

import hashlib
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import threading
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image
from pathlib import Path

MAX_ASSET_BYTES = 6 * 1024 * 1024
MAX_TOTAL_BYTES = 96 * 1024 * 1024
MAX_IMAGE_DIMENSION = 5000
MAX_DECODED_PIXELS = 28_000_000
_HOST = "web.poecdn.com"
_POB_SOURCE_COMMIT = "5606cc64fa9f15694e69001a52b90aa968d01a86"
_POB_TREE_DIRS = {"3_29": "3_29", "3_30": "3_30"}
# Skill atlas URLs can retain their dimensions while the CDN replaces old crops.
# These digests bind the two skill sheets and connector sheet to each pinned
# PoB tree directory that supplies the crop coordinates.
_VERSIONED_ATLAS_SHA256 = {
    "3_29": {
        "skills-3.jpg": "840d0b3a701c3c129e9fc8d82b688df461490624d435902d4f1cf1b28cd531e3",
        "skills-disabled-3.jpg": "28a5200d347eb488ed6a57a8e3b51c61716ec9fbb28bd776030cb3c286ec898a",
        "line-3.png": "2fb268a4b7316b822d73660bb47fcfdccd4b1ca5c47df211a8301a1a2785d0b8",
    },
    "3_30": {
        "skills-3.jpg": "314342fec3028206fc41d2a1a98c77fe4999fe942147bba96e76adce6ec72b78",
        "skills-disabled-3.jpg": "398173bf9b877ea134141150cff1c5c7bb3aa6897380d81419954915d6263165",
        "line-3.png": "e1bcf8436e11a91ad6ca620eee60bdcba2847ad9538ec688c7107a35c650457e",
    },
}


class _CDNRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urllib.parse.urlsplit(newurl)
        if (parts.scheme != "https" or parts.hostname != _HOST or parts.port is not None
                or not parts.path.startswith("/image/passive-skill/") or parts.username or parts.password):
            raise urllib.error.URLError("Passive-tree image redirect left the official CDN")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _trusted_url(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
        return (parts.scheme == "https" and parts.hostname == _HOST and parts.port is None
                and parts.path.startswith("/image/passive-skill/") and not parts.username and not parts.password)
    except ValueError:
        return False


def _pob_source_url(graph: dict, url: str) -> str | None:
    """Fallback to the pinned PoB-shipped versioned atlas if GGG's old CDN URL was replaced."""
    version_dir = _POB_TREE_DIRS.get(graph.get("version"))
    parts = urllib.parse.urlsplit(url)
    filename = parts.path.rsplit("/", 1)[-1]
    if not _trusted_url(url) or not version_dir or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", filename):
        return None
    return (f"https://raw.githubusercontent.com/PathOfBuildingCommunity/PathOfBuilding/"
            f"{_POB_SOURCE_COMMIT}/src/TreeData/{version_dir}/{filename}")


class _PoBSourceRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urllib.parse.urlsplit(newurl)
        prefix = f"/PathOfBuildingCommunity/PathOfBuilding/{_POB_SOURCE_COMMIT}/src/TreeData/"
        if (parts.scheme != "https" or parts.hostname != "raw.githubusercontent.com" or parts.port is not None
                or not parts.path.startswith(prefix) or parts.username or parts.password):
            raise urllib.error.URLError("The pinned PoB artwork redirect left its versioned source path")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _cache_path(cache_dir: Path, version: str, url: str) -> Path:
    digest = hashlib.sha256(f"{version}\0{url}".encode("utf-8")).hexdigest()
    return cache_dir / version / digest


def _expected_digest(version: str, url: str) -> str | None:
    filename = urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]
    return _VERSIONED_ATLAS_SHA256.get(version, {}).get(filename)


def _valid_image(content: bytes, size: tuple[int, int], expected_sha256: str | None = None) -> bool:
    try:
        with Image.open(BytesIO(content)) as image:
            if image.width > MAX_IMAGE_DIMENSION or image.height > MAX_IMAGE_DIMENSION:
                return False
            if (image.width, image.height) != size:
                return False
            image.verify()
        if expected_sha256 and hashlib.sha256(content).hexdigest() != expected_sha256:
            return False
        return True
    except Exception:
        return False


def load_tree_atlases(graph: dict, cache_dir: Path) -> tuple[dict[str, bytes], list[str]]:
    """Read/download exact versioned atlas URLs, enforcing per-image and aggregate bounds."""
    urls = list(dict.fromkeys(sprite["filename"] for sprite in graph["sprites"].values()))
    if any(not _trusted_url(url) for url in urls):
        return {}, ["The pinned tree data contains an image URL outside the official CDN."]
    cache_available = True
    cache_warning = None
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        cache_available = False
        cache_warning = f"Artwork cache is unavailable ({exc}); downloaded artwork will be used without caching."
    expected_sizes = {sprite["filename"]: (sprite["w"], sprite["h"])
                      for sprite in graph["sprites"].values()}
    if sum(width * height for width, height in set(expected_sizes.values())) > MAX_DECODED_PIXELS:
        return {}, ["The pinned passive-tree artwork exceeds the 28 million pixel decode limit."]

    def read_one(url: str) -> tuple[str, bytes | None, str | None]:
        path = _cache_path(cache_dir, graph["version"], url)
        expected_digest = _expected_digest(graph["version"], url)
        content = None
        try:
            if not cache_available:
                raise OSError("cache unavailable")
            if path.is_file() and path.stat().st_size <= MAX_ASSET_BYTES:
                content = path.read_bytes()
                if not _valid_image(content, expected_sizes[url], expected_digest):
                    path.unlink(missing_ok=True)
                    content = None
        except OSError:
            content = None
        if content is None:
            try:
                headers = {
                    "User-Agent": "WraeclastCompanion/0.1 (Path of Exile passive tree artwork)",
                    "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*;q=0.5",
                }
                opener = urllib.request.build_opener(_CDNRedirect())
                request = urllib.request.Request(url, headers=headers)
                with opener.open(request, timeout=10) as response:
                    if not _trusted_url(response.geturl()):
                        raise urllib.error.URLError("Image response left the official CDN")
                    advertised = response.headers.get("Content-Length")
                    if advertised and int(advertised) > MAX_ASSET_BYTES:
                        raise ValueError("image exceeds the 6 MiB download limit")
                    content = response.read(MAX_ASSET_BYTES + 1)
                if len(content) > MAX_ASSET_BYTES:
                    raise ValueError("image exceeds the 6 MiB download limit")
                if not _valid_image(content, expected_sizes[url], expected_digest):
                    raise ValueError("image is invalid or its dimensions/content digest do not match the pinned export")
            except Exception as primary_error:
                fallback_url = _pob_source_url(graph, url)
                if not fallback_url:
                    return url, None, f"{urllib.parse.urlsplit(url).path.rsplit('/', 1)[-1]}: {primary_error}"
                try:
                    opener = urllib.request.build_opener(_PoBSourceRedirect())
                    request = urllib.request.Request(fallback_url, headers=headers)
                    with opener.open(request, timeout=10) as response:
                        final = urllib.parse.urlsplit(response.geturl())
                        if (final.scheme != "https" or final.hostname != "raw.githubusercontent.com"
                                or final.port is not None or not final.path.startswith(
                                    f"/PathOfBuildingCommunity/PathOfBuilding/{_POB_SOURCE_COMMIT}/src/TreeData/{_POB_TREE_DIRS[graph['version']]}/")):
                            raise urllib.error.URLError("Pinned source response left its versioned tree directory")
                        content = response.read(MAX_ASSET_BYTES + 1)
                    if len(content) > MAX_ASSET_BYTES or not _valid_image(content, expected_sizes[url], expected_digest):
                        raise ValueError("pinned source image is invalid or has mismatched dimensions/content digest")
                except Exception as fallback_error:
                    return url, None, f"{urllib.parse.urlsplit(url).path.rsplit('/', 1)[-1]}: CDN: {primary_error}; pinned PoB versioned fallback: {fallback_error}"
            try:
                if cache_available:
                    try:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        tmp = path.with_name(path.name + f".{threading.get_ident()}.tmp")
                        tmp.write_bytes(content)
                        os.replace(tmp, path)
                    except OSError as exc:
                        return url, content, f"{path.name}: artwork loaded but could not be cached ({exc})"
            except Exception as exc:
                return url, content, f"{path.name}: artwork loaded but could not be cached ({exc})"
        return url, content, None

    images: dict[str, bytes] = {}
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=6, thread_name_prefix="tree-art") as pool:
        futures = [pool.submit(read_one, url) for url in urls]
        results = [future.result() for future in as_completed(futures)]
    total = 0
    for url, content, error in results:
        if error:
            errors.append(error)
            if content is None:
                continue
        if total + len(content) > MAX_TOTAL_BYTES:
            errors.append("Artwork cache load stopped at its 96 MiB per-tree limit.")
            break
        total += len(content)
        images[url] = content
    if cache_warning:
        errors.append(cache_warning)
    return images, errors
