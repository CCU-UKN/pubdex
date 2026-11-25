"""Offline verification of complete downloads and store-independent config identity."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

from verify_release import config_digest, required_downloads, verify_config

SCRIPT = Path(__file__).resolve().parents[1] / "verify_release.py"
VERSION = "0.2.0"


@pytest.mark.parametrize("damage", [None, "missing", "corrupt", "missing checksum"])
def test_download_completeness_uses_the_committed_checksums(tmp_path, damage):
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    names = required_downloads(VERSION)
    # The authority lives outside the downloads. IMAGES is listed but absent.
    entries = []
    for name in (*names, "IMAGES"):
        content = f"synthetic {name}".encode()
        entries.append(f"{hashlib.sha256(content).hexdigest()}  {name}\n")
        if name != "IMAGES":
            (downloads / name).write_bytes(content)
    checksums = tmp_path / "SHA256SUMS"
    checksums.write_text("".join(entries))
    if damage == "missing":
        for name in names[1:]:
            (downloads / name).unlink()
    elif damage == "corrupt":
        (downloads / names[0]).write_bytes(b"corrupt")
    elif damage == "missing checksum":
        checksums.write_text("".join(entries[1:]))
    result = subprocess.run([sys.executable, "-I", str(SCRIPT), "files", "--version", VERSION,
                             "--checksums", str(checksums)], cwd=downloads, capture_output=True, text=True)
    assert result.returncode == (0 if damage is None else 1), result.stderr
    if damage is None:
        assert result.stdout.count(": OK") == 5 and "IMAGES" not in result.stdout
    else:
        assert "verify_release.py:" in result.stderr


@pytest.mark.parametrize("store", ["classic", "containerd"])
@pytest.mark.parametrize("format_version", [1, 2])
def test_saved_config_is_checked_independently_of_the_builder_store(tmp_path, store, format_version):
    config = b'{"architecture":"amd64","os":"linux","config":{"Labels":{"synthetic":"yes"}}}'
    digest = "sha256:" + hashlib.sha256(config).hexdigest()
    image_id = digest if store == "classic" else "sha256:" + "a" * 64
    member = "config.json" if store == "classic" else f"blobs/sha256/{digest[7:]}"
    archive = tmp_path / "saved.tar"
    with tarfile.open(archive, "w") as saved:
        for name, content in (("manifest.json", json.dumps([{"Config": member}]).encode()), (member, config)):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            saved.addfile(info, io.BytesIO(content))
    assert config_digest(archive) == digest
    image = {"id": image_id}
    if format_version == 2:
        image["config_digest"] = digest
    record = {"format": f"pubdex-provenance/{format_version}", "build": {"image_store": store},
              "images": {"application": image}}
    provenance = tmp_path / "provenance.json"
    provenance.write_text(json.dumps(record))
    if format_version == 1 and store == "containerd":
        with pytest.raises(ValueError, match="legacy containerd"):
            verify_config(archive, provenance, "application")
        return
    assert verify_config(archive, provenance, "application") == digest
    image["config_digest" if format_version == 2 else "id"] = "sha256:" + "b" * 64
    provenance.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="!="):
        verify_config(archive, provenance, "application")
