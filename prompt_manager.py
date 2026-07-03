"""
Prompt version manager for bill extraction prompts.

Discovers versioned prompt files (bill_extraction_v{N}.txt) and
provides the latest or a specific version on request.
"""

import re
from pathlib import Path

import attr


@attr.s(auto_attribs=True, kw_only=True)
class PromptManager:
    """Manages versioned bill extraction prompts."""

    prompts_dir: Path = attr.ib(factory=lambda: Path(__file__).parent / "prompts")
    _versions: dict[int, Path] = attr.ib(init=False, factory=dict)
    _latest_version: int = attr.ib(init=False, default=0)

    def __attrs_post_init__(self):
        pattern = re.compile(r"^bill_extraction_v(\d+)\.txt$")
        for path in self.prompts_dir.iterdir():
            match = pattern.match(path.name)
            if match:
                version = int(match.group(1))
                self._versions[version] = path

        if not self._versions:
            raise FileNotFoundError(
                f"No bill_extraction_v*.txt files found in {self.prompts_dir}"
            )

        self._latest_version = max(self._versions)

    @property
    def latest_version(self) -> int:
        return self._latest_version

    def list_versions(self) -> list[int]:
        """Return sorted list of available prompt versions."""
        return sorted(self._versions.keys())

    def get_prompt(self, version: int | None = None) -> str:
        """
        Get prompt text for a specific version.

        Args:
            version: Prompt version number. None = latest.

        Returns:
            Prompt template text.

        Raises:
            ValueError: If the requested version doesn't exist.
        """
        v = version if version is not None else self._latest_version
        if v not in self._versions:
            available = ", ".join(str(x) for x in self.list_versions())
            raise ValueError(
                f"Prompt version {v} not found. Available: {available}"
            )
        print(f"Using prompt version: v{v}")
        return self._versions[v].read_text()
