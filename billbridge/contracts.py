"""Mock contract lookup service. Stands in for the Chroma vector index planned for M3."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

SECTION_HEADING = re.compile(r"^###\s+(\d+(?:\.\d+)*)\s+(.+)$")
WORD = re.compile(r"[a-z0-9]+")


@dataclass
class ContractSection:
    section_id: str
    title: str
    text: str


def parse_sections(markdown: str) -> list[ContractSection]:
    sections: list[ContractSection] = []
    current: ContractSection | None = None
    for line in markdown.splitlines():
        match = SECTION_HEADING.match(line)
        if match:
            current = ContractSection(match.group(1), match.group(2).strip(), "")
            sections.append(current)
        elif current and not line.startswith("#"):
            current.text = f"{current.text} {line.strip()}".strip()
    return sections


class ContractLibrary:
    def __init__(self, data_dir: Path, clients: dict):
        self._sections: dict[str, list[ContractSection]] = {}
        for client_id, rules in clients.items():
            path = data_dir / rules["contract"]
            self._sections[client_id] = parse_sections(path.read_text()) if path.exists() else []

    def sections(self, client_id: str) -> list[ContractSection]:
        return self._sections.get(client_id, [])

    def retrieve(self, client_id: str, query: str, k: int = 3) -> list[ContractSection]:
        """Keyword-overlap ranking; good enough to exercise the retrieval interface."""
        terms = set(WORD.findall(query.lower()))
        scored = []
        for index, section in enumerate(self.sections(client_id)):
            words = set(WORD.findall(f"{section.title} {section.text}".lower()))
            scored.append((len(terms & words), -index, section))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [section for score, _, section in scored[:k] if score > 0]
