"""Load and verify the packaged operating instructions."""

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path


_HARNESSES = Path(__file__).with_name("harnesses")
_MANIFEST_DIGESTS = {
    "v1": "5477e3019d30aa21e4dc7f525f973b54e769a7b51a557b4db0040a0ad97b44d9",
    "v2": "8a2d4a5b038f148e3ae8b4121ea1122e20734e08cf3a5d5cb6b5263f590b8980",
}
_MANIFEST_FIELDS = {"schema_version", "version", "instructions_sha256"}


@dataclass(frozen=True)
class Harness:
    version: str
    instructions: str
    digest: str


def harness_versions() -> tuple[str, ...]:
    return tuple(_MANIFEST_DIGESTS)


def load_harness(version: str = "v1") -> Harness:
    if not isinstance(version, str) or version not in _MANIFEST_DIGESTS:
        raise ValueError("Unsupported harness version")

    directory = _HARNESSES / version
    manifest_bytes = (directory / "manifest.json").read_bytes()
    if sha256(manifest_bytes).hexdigest() != _MANIFEST_DIGESTS[version]:
        raise ValueError(f"Harness {version} manifest digest mismatch")

    manifest = json.loads(manifest_bytes)
    if (
        not isinstance(manifest, dict)
        or manifest.keys() != _MANIFEST_FIELDS
        or type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 1
        or manifest["version"] != version
    ):
        raise ValueError(f"Invalid harness {version} manifest")

    content = (directory / "AGENT.md").read_bytes()
    digest = sha256(content).hexdigest()
    if manifest["instructions_sha256"] != digest:
        raise ValueError(f"Harness {version} instructions digest mismatch")
    return Harness(version, content.decode("utf-8"), digest)
