from __future__ import annotations

import io
import tarfile
import zipfile

import pytest

from blender_mcp.handlers.asset_repository_handler import RepositoryManager, _HttpsOnlyRedirectHandler


@pytest.mark.parametrize("member_name", ["../outside.txt", r"..\outside.txt", "C:/outside.txt"])
def test_zip_archive_rejects_paths_outside_extraction_root(tmp_path, member_name):
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(member_name, "should not escape")

    extraction_root = tmp_path / "extract"
    with pytest.raises(Exception, match="Extraction failed: archive contains an unsafe member path"):
        RepositoryManager()._extract_archive(str(archive), str(extraction_root))

    assert not (tmp_path / "outside.txt").exists()
    assert not (tmp_path / "C:" / "outside.txt").exists()


def test_tar_archive_rejects_paths_outside_extraction_root(tmp_path):
    archive = tmp_path / "unsafe.tar.gz"
    payload = b"should not escape"
    member = tarfile.TarInfo("../outside.txt")
    member.size = len(payload)
    with tarfile.open(archive, "w:gz") as output:
        output.addfile(member, io.BytesIO(payload))

    extraction_root = tmp_path / "extract"
    with pytest.raises(Exception, match="Extraction failed: archive contains an unsafe member path"):
        RepositoryManager()._extract_archive(str(archive), str(extraction_root))

    assert not (tmp_path / "outside.txt").exists()


def test_tar_archive_extracts_regular_files_within_root(tmp_path):
    archive = tmp_path / "safe.tar.gz"
    payload = b"tar payload"
    member = tarfile.TarInfo("models/scene.blend")
    member.size = len(payload)
    with tarfile.open(archive, "w:gz") as output:
        output.addfile(member, io.BytesIO(payload))

    extraction_root = tmp_path / "extract-tar"
    files = RepositoryManager()._extract_archive(str(archive), str(extraction_root))

    assert files == [str(extraction_root / "models" / "scene.blend")]
    assert (extraction_root / "models" / "scene.blend").read_bytes() == payload


def test_tar_archive_rejects_symbolic_links(tmp_path):
    archive = tmp_path / "unsafe.tar.gz"
    member = tarfile.TarInfo("link")
    member.type = tarfile.SYMTYPE
    member.linkname = "../../outside.txt"
    with tarfile.open(archive, "w:gz") as output:
        output.addfile(member)

    with pytest.raises(Exception, match="Extraction failed: archive contains an unsupported link"):
        RepositoryManager()._extract_archive(str(archive), str(tmp_path / "extract"))


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("file:///etc/passwd", "valid HTTPS URL"),
        ("https://127.0.0.1/internal.zip", "OpenGameArt.org host"),
    ],
)
def test_opengameart_rejects_untrusted_download_urls(url, message):
    with pytest.raises(Exception, match=message):
        RepositoryManager().get_opengameart_asset(url)


def test_download_redirects_must_remain_https():
    handler = _HttpsOnlyRedirectHandler()

    with pytest.raises(ValueError, match="valid HTTPS URL"):
        handler.redirect_request(None, None, 302, "Found", {}, "file:///etc/passwd")


def test_repository_asset_names_cannot_escape_temp_directory():
    with pytest.raises(ValueError, match="simple repository slug"):
        RepositoryManager().get_kenney_asset("../outside")


def test_zip_archive_extracts_regular_files_within_root(tmp_path):
    archive = tmp_path / "safe.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("models/scene.blend", b"blend")

    extraction_root = tmp_path / "extract"
    files = RepositoryManager()._extract_archive(str(archive), str(extraction_root))

    assert files == [str(extraction_root / "models" / "scene.blend")]
    assert (extraction_root / "models" / "scene.blend").read_bytes() == b"blend"
