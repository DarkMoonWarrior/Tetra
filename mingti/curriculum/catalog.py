from __future__ import annotations

import csv
import re
from collections.abc import Iterable
from pathlib import Path

from .schemas import CurriculumNode, CurriculumSearchItem, CurriculumTreeNode


class CurriculumDataError(ValueError):
    """The curriculum source violates the module invariants."""


class InMemoryCurriculumCatalog:
    """Deep curriculum module with an in-memory adapter used by tests."""

    def __init__(self, nodes: Iterable[CurriculumNode]):
        node_list = list(nodes)
        self._nodes = {node.node_id: node for node in node_list}
        if len(self._nodes) != len(node_list):
            raise CurriculumDataError("Duplicate curriculum node_id")
        self._validate_references()

    def get_node(self, node_id: str) -> CurriculumNode:
        try:
            return self._nodes[node_id]
        except KeyError as exc:
            raise KeyError(f"Unknown curriculum node: {node_id}") from exc

    def list_nodes(self) -> list[CurriculumNode]:
        return list(self._nodes.values())

    def get_tree(self) -> CurriculumTreeNode:
        roots = [node for node in self._nodes.values() if node.parent_id is None]
        if len(roots) != 1:
            raise CurriculumDataError(f"Expected exactly one root; got {len(roots)}")

        children_by_parent: dict[str, list[CurriculumNode]] = {}
        for node in self._nodes.values():
            if node.parent_id:
                children_by_parent.setdefault(node.parent_id, []).append(node)

        def build(node: CurriculumNode) -> CurriculumTreeNode:
            children = sorted(children_by_parent.get(node.node_id, []), key=self._sort_key)
            return CurriculumTreeNode(**node.model_dump(), children=[build(child) for child in children])

        return build(roots[0])

    def search(
        self,
        query: str = "",
        *,
        volume: str | None = None,
        node_types: set[str] | None = None,
        limit: int | None = 30,
    ) -> list[CurriculumSearchItem]:
        """Search selectable curriculum nodes with their human-readable path."""
        allowed_types = node_types or {"knowledge_point"}
        normalized_query = self._normalize(query)
        query_terms = self._query_terms(query)
        results: list[CurriculumSearchItem] = []
        for node in self._nodes.values():
            if node.node_type not in allowed_types or (volume and node.volume != volume):
                continue
            path = self.path_for(node.node_id)
            searchable = " ".join(
                [
                    node.code,
                    node.name,
                    node.description,
                    path.get("chapter", ""),
                    path.get("section", ""),
                    *node.typical_question_types,
                    *node.common_errors,
                ]
            )
            normalized_searchable = self._normalize(searchable)
            if normalized_query and normalized_query not in normalized_searchable:
                if not query_terms or not all(term in normalized_searchable for term in query_terms):
                    continue
            score = self._search_score(node, normalized_query, query_terms, normalized_searchable)
            results.append(
                CurriculumSearchItem(
                    node_id=node.node_id,
                    code=node.code,
                    name=node.name,
                    node_type=node.node_type,
                    volume=node.volume,
                    chapter=path.get("chapter") or None,
                    section=path.get("section") or None,
                    description=node.description,
                    primary_competencies=node.primary_competencies,
                    gaokao_priority=node.gaokao_priority,
                    match_score=score,
                )
            )
        ordered = sorted(
            results,
            key=lambda item: (-item.match_score, self._sort_key(self._nodes[item.node_id])),
        )
        return ordered if limit is None else ordered[:limit]

    def path_for(self, node_id: str) -> dict[str, str]:
        node = self.get_node(node_id)
        path: dict[str, str] = {}
        current: CurriculumNode | None = node
        while current is not None:
            if current.node_type == "volume":
                path["volume"] = current.name
            elif current.node_type == "chapter":
                path["chapter"] = current.name
            elif current.node_type == "section":
                path["section"] = current.name
            if current.parent_id is None:
                break
            current = self.get_node(current.parent_id)
        path.setdefault("volume", node.volume)
        return path

    def _validate_references(self) -> None:
        for node in self._nodes.values():
            if node.parent_id and node.parent_id not in self._nodes:
                raise CurriculumDataError(f"Missing parent {node.parent_id} for {node.node_id}")
            missing = [item for item in node.prerequisite_node_ids if item not in self._nodes]
            if missing:
                raise CurriculumDataError(f"Missing prerequisites for {node.node_id}: {missing}")

    @staticmethod
    def _sort_key(node: CurriculumNode) -> tuple[int, ...]:
        parts = []
        for part in node.code.split("."):
            try:
                parts.append(int(part))
            except ValueError:
                parts.append(999)
        return tuple(parts)

    @staticmethod
    def _normalize(value: str) -> str:
        return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value.lower())

    @classmethod
    def _query_terms(cls, value: str) -> list[str]:
        terms = [cls._normalize(item) for item in re.split(r"[\s,，、;；]+", value)]
        return [item for item in terms if item]

    @classmethod
    def _search_score(
        cls,
        node: CurriculumNode,
        normalized_query: str,
        query_terms: list[str],
        normalized_searchable: str,
    ) -> float:
        if not normalized_query:
            return 0.5
        normalized_name = cls._normalize(node.name)
        normalized_code = cls._normalize(node.code)
        if normalized_query == normalized_name or normalized_query == normalized_code:
            return 1.0
        if normalized_query in normalized_name:
            return 0.92
        if normalized_name and normalized_name in normalized_query:
            return 0.86
        coverage = sum(term in normalized_searchable for term in query_terms) / max(1, len(query_terms))
        if normalized_query in normalized_searchable:
            return round(min(0.84, 0.62 + len(normalized_query) / 100), 2)
        return round(min(0.78, 0.45 + coverage * 0.3), 2)


class CsvCurriculumCatalog(InMemoryCurriculumCatalog):
    """Production adapter that loads the reviewed curriculum CSV."""

    def __init__(self, path: Path):
        if not path.exists():
            raise CurriculumDataError(f"Curriculum file not found: {path}")
        with path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        super().__init__(self._from_row(row) for row in rows)

    @staticmethod
    def _from_row(row: dict[str, str]) -> CurriculumNode:
        def split(field: str) -> list[str]:
            return [item for item in row.get(field, "").split("|") if item]

        return CurriculumNode(
            node_id=row["node_id"],
            parent_id=row["parent_id"] or None,
            volume=row["volume"],
            node_type=row["node_type"],
            code=row["code"],
            name=row["name"],
            description=row["description"],
            prerequisite_node_ids=split("prerequisite_node_ids"),
            primary_competencies=split("primary_competencies"),
            typical_question_types=split("typical_question_types"),
            common_errors=split("common_errors"),
            gaokao_priority=row["gaokao_priority"],
            status=row["status"],
            reviewed_by=row["reviewed_by"],
        )
