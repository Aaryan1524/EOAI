"""Load and verify the packaged operating instructions."""

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path


_HARNESSES = Path(__file__).with_name("harnesses")
_MANIFEST_DIGESTS = {
    "v1": "5477e3019d30aa21e4dc7f525f973b54e769a7b51a557b4db0040a0ad97b44d9",
    "v2": "8a2d4a5b038f148e3ae8b4121ea1122e20734e08cf3a5d5cb6b5263f590b8980",
    "v3": "7a036a4219851ed10663adbe0850ea46aa500b232e36c322009c169694c1ebbe",
}
_MANIFEST_FIELDS = {"schema_version", "version", "instructions_sha256"}


@dataclass(frozen=True)
class Harness:
    version: str
    instructions: str
    digest: str


@dataclass(frozen=True)
class HarnessContent:
    """The complete editable surface of an experimental harness."""

    instructions: str
    skills: str = ""
    memory: str = ""

    def __post_init__(self) -> None:
        for name, value in self.as_dict().items():
            if type(value) is not str or len(value.encode("utf-8")) > 32_768:
                raise ValueError(f"Invalid harness text: {name}")
        if not self.instructions.strip():
            raise ValueError("Harness instructions must not be empty")

    def as_dict(self) -> dict[str, str]:
        return {"instructions": self.instructions, "skills": self.skills, "memory": self.memory}

    @property
    def digest(self) -> str:
        content = json.dumps(self.as_dict(), sort_keys=True, ensure_ascii=False)
        return sha256(content.encode("utf-8")).hexdigest()

    def render(self) -> str:
        sections = [self.instructions]
        if self.skills:
            sections.append(f"## Skills\n{self.skills}")
        if self.memory:
            sections.append(f"## Memory\n{self.memory}")
        return "\n\n".join(sections)


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
