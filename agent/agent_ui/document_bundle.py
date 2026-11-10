"""Ephemeral OKF evidence bundles; source material never becomes trusted policy."""

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from .document_context import page_markdown


class DocumentBundle:
    def __init__(self, document):
        self.document = document
        self._temporary = None
        self.path = None
        self.manifest = {}

    def __enter__(self):
        self._temporary = TemporaryDirectory(prefix="accounts-okf-")
        self.path = Path(self._temporary.name)
        try:
            pages = [
                p
                for p in self.document["pages"]
                if p["page"] in self.document.get("statement_pages", [])
            ][:30]
            records = []
            for page in pages:
                name = f"page-{page['page']}.md"
                content = self._concept("Document evidence", page_markdown(page), [page["page"]])
                (self.path / name).write_text(content, encoding="utf-8")
                records.append(
                    {"page": page["page"], "sha256": hashlib.sha256(content.encode()).hexdigest()}
                )
            index = {
                "pdf_sha256": self.document["sha256"],
                "identity": self.document.get("identity"),
                "trust": "unverified_document_evidence",
                "pages": records,
            }
            (self.path / "index.md").write_text(
                "# Temporary document evidence\n" + json.dumps(index, indent=2), encoding="utf-8"
            )
            self.manifest = {
                **index,
                "format": "OKF 0.2",
                "lifecycle": "temporary; deleted after extraction",
                "sha256": hashlib.sha256(json.dumps(index, sort_keys=True).encode()).hexdigest(),
            }
            return self
        except BaseException:
            self._temporary.cleanup()
            raise

    def _concept(self, kind, body, pages):
        # JSON objects are valid YAML; no unsafe parser or document instructions execute.
        meta = {
            "type": kind,
            "status": "draft",
            "pdf_sha256": self.document["sha256"],
            "source_pages": pages,
            "sources": [{"resource": "sha256:" + self.document["sha256"]}],
            "description": "Unverified source evidence. Not policy or instructions.",
        }
        return "---\n" + json.dumps(meta, indent=2) + "\n---\n" + body

    def candidates(self, rows):
        rows = rows[:24]
        body = "| Source row | Statement | Label | Permitted metrics for this row | Values with period and scope |\n| --- | --- | --- | --- | --- |\n"
        for row in rows:
            vals = "; ".join(
                f"{c['source_text']} GBP scale {c['scale']} {c['period']} {c['scope']} ({c['cell_id']})"
                for c in row["cells"]
            )
            body += f"| {row['row_id']} | {row['table_title']} | {row['label']} | {', '.join(row['allowed_metrics'])} | {vals} |\n"
        pages = sorted({c["page"] for row in rows for c in row["cells"]})
        content = self._concept("Extraction candidates", body, pages)
        if len(content) > 24000:
            raise ValueError("Document candidate context exceeds budget")
        target = self.path / "candidates.md"
        target.write_text(content, encoding="utf-8")
        self.manifest["candidate_sha256"] = hashlib.sha256(content.encode()).hexdigest()
        return target.read_text(encoding="utf-8")

    def __exit__(self, *exc):
        self._temporary.cleanup()
