"""Progressive discovery and loading of local SKILL.md instructions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_WORD_PATTERN = re.compile(r"[a-z0-9_-]+", re.IGNORECASE)
_CJK_PATTERN = re.compile(r"[\u4e00-\u9fff]+")
_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


@dataclass(frozen=True, slots=True)
class SkillMetadata:
    name: str
    description: str
    path: Path


@dataclass(frozen=True, slots=True)
class LoadedSkill:
    metadata: SkillMetadata
    instructions: str


@dataclass(frozen=True, slots=True)
class SkillSelection:
    loaded: tuple[LoadedSkill, ...]
    catalog_size: int
    metadata_chars: int
    loaded_chars: int

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(item.metadata.name for item in self.loaded)

    def prompt_blocks(self) -> tuple[str, ...]:
        return tuple(
            (
                f"Skill: {item.metadata.name}\n"
                f"Purpose: {item.metadata.description}\n"
                f"Instructions:\n{item.instructions}"
            )
            for item in self.loaded
        )


class SkillCatalog:
    """Read cheap metadata first, then load only task-relevant skill bodies."""

    def __init__(
        self,
        roots: list[Path] | tuple[Path, ...],
        *,
        max_metadata_bytes: int = 8_192,
        max_body_bytes: int = 65_536,
    ) -> None:
        if not roots:
            raise ValueError("at least one skill root is required")
        self.roots = tuple(root.resolve() for root in roots)
        self.max_metadata_bytes = max_metadata_bytes
        self.max_body_bytes = max_body_bytes
        self._metadata: tuple[SkillMetadata, ...] | None = None

    def scan(self) -> tuple[SkillMetadata, ...]:
        found: list[SkillMetadata] = []
        names: set[str] = set()
        for root in self.roots:
            if not root.exists():
                continue
            for path in sorted(root.glob("*/SKILL.md")):
                resolved = path.resolve()
                self._require_inside_root(resolved)
                fields = self._read_frontmatter(resolved)
                name = fields.get("name", "").strip()
                description = fields.get("description", "").strip()
                if not _NAME_PATTERN.fullmatch(name):
                    raise ValueError(f"invalid skill name in {resolved}: {name!r}")
                if not description:
                    raise ValueError(f"missing skill description in {resolved}")
                if name in names:
                    raise ValueError(f"duplicate skill name: {name}")
                names.add(name)
                found.append(SkillMetadata(name, description, resolved))
        self._metadata = tuple(found)
        return self._metadata

    @property
    def metadata(self) -> tuple[SkillMetadata, ...]:
        return self._metadata if self._metadata is not None else self.scan()

    def select(self, request: str, *, limit: int = 2) -> SkillSelection:
        if limit < 1:
            raise ValueError("skill selection limit must be positive")
        request_terms = self._terms(request)
        scored: list[tuple[int, str, SkillMetadata]] = []
        for item in self.metadata:
            haystack = f"{item.name} {item.description}".lower()
            overlap = len(request_terms & self._terms(haystack))
            phrase_bonus = 3 if item.name.lower().replace("-", " ") in request.lower() else 0
            score = overlap + phrase_bonus
            if score > 0:
                scored.append((score, item.name, item))

        selected = [item for _, _, item in sorted(scored, key=lambda row: (-row[0], row[1]))]
        loaded = tuple(self.load(item) for item in selected[:limit])
        return SkillSelection(
            loaded=loaded,
            catalog_size=len(self.metadata),
            metadata_chars=sum(len(item.name) + len(item.description) for item in self.metadata),
            loaded_chars=sum(len(item.instructions) for item in loaded),
        )

    def load(self, metadata: SkillMetadata) -> LoadedSkill:
        resolved = metadata.path.resolve()
        self._require_inside_root(resolved)
        size = resolved.stat().st_size
        if size > self.max_body_bytes:
            raise ValueError(f"skill file exceeds {self.max_body_bytes} bytes: {resolved}")
        text = resolved.read_text(encoding="utf-8")
        _, body = self._split_frontmatter(text, resolved)
        instructions = body.strip()
        if not instructions:
            raise ValueError(f"skill body is empty: {resolved}")
        return LoadedSkill(metadata, instructions)

    def _read_frontmatter(self, path: Path) -> dict[str, str]:
        consumed = 0
        lines: list[str] = []
        with path.open("r", encoding="utf-8") as file:
            first = file.readline()
            consumed += len(first.encode("utf-8"))
            if first.strip() != "---":
                raise ValueError(f"skill must start with YAML-like frontmatter: {path}")
            for line in file:
                consumed += len(line.encode("utf-8"))
                if consumed > self.max_metadata_bytes:
                    raise ValueError(
                        f"skill metadata exceeds {self.max_metadata_bytes} bytes: {path}"
                    )
                if line.strip() == "---":
                    return self._parse_fields(lines, path)
                lines.append(line)
        raise ValueError(f"unterminated skill frontmatter: {path}")

    @staticmethod
    def _split_frontmatter(text: str, path: Path) -> tuple[dict[str, str], str]:
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            raise ValueError(f"skill must start with YAML-like frontmatter: {path}")
        for index, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                return SkillCatalog._parse_fields(lines[1:index], path), "\n".join(
                    lines[index + 1 :]
                )
        raise ValueError(f"unterminated skill frontmatter: {path}")

    @staticmethod
    def _parse_fields(lines: list[str], path: Path) -> dict[str, str]:
        fields: dict[str, str] = {}
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                raise ValueError(f"invalid skill metadata line in {path}: {raw_line!r}")
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip().strip("'\"")
        return fields

    def _require_inside_root(self, path: Path) -> None:
        if not any(path.is_relative_to(root) for root in self.roots):
            raise ValueError(f"skill path escapes configured roots: {path}")

    @staticmethod
    def _terms(text: str) -> set[str]:
        terms = {match.group(0).lower() for match in _WORD_PATTERN.finditer(text)}
        for match in _CJK_PATTERN.finditer(text):
            phrase = match.group(0)
            if len(phrase) == 1:
                terms.add(phrase)
            else:
                terms.update(phrase[index : index + 2] for index in range(len(phrase) - 1))
        return terms
