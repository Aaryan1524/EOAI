import json
from hashlib import sha256
from shutil import copytree

import pytest

from enterprise_ops.agent import harness
from enterprise_ops.agent.harness import harness_versions, load_harness


def test_packaged_harness_versions():
    assert harness_versions() == ("v1", "v2")
    v1 = load_harness()
    v2 = load_harness("v2")
    assert v1 == load_harness("v1")
    assert v1.version == "v1"
    assert v1.digest == "f85326520bb24f76bb47602bd3620d88281ccde5c775254a553b4aa097a5d255"
    assert v2.version == "v2"
    assert v2.instructions != v1.instructions
    assert v2.digest != v1.digest
    for installed in (v1, v2):
        assert installed.digest == sha256(installed.instructions.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("version", ["v3", "V1", "../v1", "v1/../v2", "/v1", ""])
def test_rejects_unsupported_harness_versions(version):
    with pytest.raises(ValueError, match="Unsupported harness version"):
        load_harness(version)


@pytest.fixture
def copied_harnesses(tmp_path, monkeypatch):
    directory = tmp_path / "harnesses"
    copytree(harness._HARNESSES, directory)
    monkeypatch.setattr(harness, "_HARNESSES", directory)
    return directory


def test_rejects_corrupted_harness_instructions(copied_harnesses):
    source = copied_harnesses / "v2" / "AGENT.md"
    source.write_bytes(source.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="instructions digest mismatch"):
        load_harness("v2")


def test_rejects_rehashed_harness_with_modified_manifest(copied_harnesses):
    directory = copied_harnesses / "v2"
    instructions = directory / "AGENT.md"
    content = instructions.read_bytes() + b"\n"
    instructions.write_bytes(content)
    manifest_file = directory / "manifest.json"
    manifest = json.loads(manifest_file.read_bytes())
    manifest["instructions_sha256"] = sha256(content).hexdigest()
    manifest_file.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="manifest digest mismatch"):
        load_harness("v2")
