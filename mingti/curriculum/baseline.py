from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


class CurriculumBaselineError(RuntimeError):
    pass


@dataclass(frozen=True)
class CurriculumBaseline:
    baseline_id: str
    standard_title: str
    textbook_edition: str
    catalog_path: Path
    manifest_path: Path

    @classmethod
    def load(cls, manifest_path: Path, catalog_path: Path) -> "CurriculumBaseline":
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CurriculumBaselineError(f"课程基线清单无法读取：{exc}") from exc
        # KMI-01: normalize line endings before hashing so the sealed baseline
        # hash is platform-independent (LF and CRLF checkouts verify equal).
        catalog_bytes = catalog_path.read_bytes()
        canonical_bytes = catalog_bytes.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        actual = hashlib.sha256(canonical_bytes).hexdigest()
        expected = str(manifest.get("catalog_sha256", "")).lower()
        if not expected or actual != expected:
            raise CurriculumBaselineError("人教 A 版课程目录已偏离封存基线，已拒绝用于教案生成")
        sources = manifest.get("official_sources") or []
        standard = next((item for item in sources if item.get("authority") == "中华人民共和国教育部"), None)
        if not standard:
            raise CurriculumBaselineError("课程基线缺少教育部课程标准来源")
        return cls(
            baseline_id=str(manifest["baseline_id"]),
            standard_title=str(standard["title"]),
            textbook_edition=str(manifest.get("textbook_edition", "人教 A 版（2019）")),
            catalog_path=catalog_path,
            manifest_path=manifest_path,
        )
