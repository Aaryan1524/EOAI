"""Load the packaged operating instructions."""

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path


@dataclass(frozen=True)
class Harness:
    version: str
    instructions: str
    digest: str


def load_harness(version: str = "v1") -> Harness:
    if version != "v1":
        raise ValueError("Unsupported harness version")
    source = Path(__file__).with_name("harnesses") / version / "AGENT.md"
    content = source.read_bytes()
    return Harness(version, content.decode("utf-8"), sha256(content).hexdigest())
