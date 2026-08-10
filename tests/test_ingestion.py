from pathlib import Path

import pytest

from knowledge.ingestion import IngestionError, load_document, load_markdown, split_markdown
from knowledge.models import ChunkMetadata, EntityType, RelationType, SourceReference


def test_load_markdown_builds_entities_relationships_and_stable_chunks(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ambitious-card.md"
    path.write_text(
        """---
title: Ambitious Card Notes
author: Example Researcher
category: card magic
technique: [Double Lift]
psychology: Time Misdirection
performer: [Example Performer]
effect: Ambitious Card
method: Secret Reversal
entities:
  - type: technique
    name: Tilt
relationships:
  - source: Ambitious Card
    target: Double Lift
    type: uses_technique
    evidence: A possible handling.
---
# Overview

The selected card repeatedly rises to the top.

## Construction

Time separation can weaken the audience's reconstruction.
""",
        encoding="utf-8",
    )

    chunks = load_markdown(path, chunk_size=120, chunk_overlap=20)
    repeated = load_markdown(path, chunk_size=120, chunk_overlap=20)

    assert chunks
    assert [chunk.id for chunk in chunks] == [chunk.id for chunk in repeated]
    metadata = chunks[0].metadata
    assert metadata.title == "Ambitious Card Notes"
    assert metadata.technique == ["Double Lift"]
    assert {entity.type for entity in metadata.entities} == {
        EntityType.EFFECT,
        EntityType.TECHNIQUE,
        EntityType.METHOD,
        EntityType.PSYCHOLOGY_PRINCIPLE,
        EntityType.PERFORMER,
        EntityType.SOURCE,
    }
    assert any(
        relationship.type == RelationType.USES
        for relationship in metadata.relationships
    )
    assert sum(
        relationship.type == RelationType.EXPLAINS
        for relationship in metadata.relationships
    ) == len(metadata.entities) - 1
    assert chunks[0].annotations.magic_category == "card magic"
    assert chunks[0].annotations.sources[0].source_id == metadata.source_id


def test_unknown_relationship_endpoint_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.md"
    path.write_text(
        """---
title: Broken Notes
effect: Known Effect
relationships:
  - source: Known Effect
    target: Missing Technique
    type: uses_technique
---
# Text
Content.
""",
        encoding="utf-8",
    )

    with pytest.raises(IngestionError, match="does not match an entity"):
        load_markdown(path)


def test_split_markdown_preserves_headings_and_limits_chunks() -> None:
    markdown = "# First\n\n" + ("word " * 30) + "\n\n## Second\n\nShort text."
    chunks = split_markdown(markdown, chunk_size=60, chunk_overlap=10)

    assert len(chunks) >= 3
    assert chunks[0][0] == "First"
    assert chunks[-1] == ("Second", "Second\n\nShort text.")
    assert all(len(text) <= 60 for _, text in chunks)


def test_plain_text_and_chunk_metadata_extractor_are_supported(tmp_path: Path) -> None:
    class FakeMetadataExtractor:
        def extract(self, text, document, source_locator):
            return ChunkMetadata(
                magic_category="mentalism",
                techniques=["Equivoque"],
                psychological_principles=["Choice Architecture"],
                performers=["Example Performer"],
                sources=[SourceReference(title=document.title, locator=source_locator)],
                extraction_method="glm",
                confidence=0.87,
            )

    path = tmp_path / "notes.txt"
    path.write_text("A participant apparently makes a free choice.", encoding="utf-8")

    chunks = load_document(path, metadata_extractor=FakeMetadataExtractor())

    assert chunks[0].annotations.magic_category == "mentalism"
    assert chunks[0].metadata.technique == ["Equivoque"]
    assert EntityType.TECHNIQUE in {item.type for item in chunks[0].metadata.entities}
    assert EntityType.PSYCHOLOGY_PRINCIPLE in {
        item.type for item in chunks[0].metadata.entities
    }
    assert chunks[0].to_payload()["metadata_extraction"] == "glm"


def test_pdf_extraction_preserves_page_numbers(tmp_path: Path) -> None:
    from reportlab.pdfgen.canvas import Canvas

    path = tmp_path / "theory.pdf"
    canvas = Canvas(str(path))
    canvas.setTitle("Theory Notes")
    canvas.setAuthor("Example Author")
    canvas.drawString(72, 720, "First page discusses misdirection.")
    canvas.showPage()
    canvas.drawString(72, 720, "Second page discusses audience memory.")
    canvas.save()

    chunks = load_document(path, chunk_size=500)

    assert [chunk.page_number for chunk in chunks] == [1, 2]
    assert [chunk.source_locator for chunk in chunks] == ["page 1", "page 2"]
    assert chunks[0].metadata.title == "Theory Notes"
    assert chunks[0].metadata.author == "Example Author"
    assert "misdirection" in chunks[0].text
