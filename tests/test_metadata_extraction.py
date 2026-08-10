from knowledge.extractors import ExtractedDocument, ExtractedSection
from knowledge.metadata_extraction import (
    GLMMetadataExtractor,
    MetadataExtractionResult,
)
from knowledge.models import SourceReference


class FakeStructuredLLM:
    def generate_structured(self, prompt, response_model, **kwargs):
        assert response_model is MetadataExtractionResult
        assert "Double Lift" in prompt
        return MetadataExtractionResult(
            magic_category="card magic",
            techniques=["Double Lift"],
            psychological_principles=["Time Misdirection"],
            performers=["Example Performer"],
            sources=[SourceReference(title="Another Work", locator="chapter 2")],
            confidence=0.91,
        )


def test_glm_metadata_is_validated_and_merged_with_declared_metadata() -> None:
    document = ExtractedDocument(
        path="notes.md",
        media_type="text/markdown",
        title="Card Notes",
        author="Researcher",
        sections=[ExtractedSection(text="Double Lift")],
        declared_metadata={"technique": ["Tilt"], "category": "close-up"},
    )
    extractor = GLMMetadataExtractor(FakeStructuredLLM())

    metadata = extractor.extract("Double Lift", document, "Methods")

    assert metadata.magic_category == "card magic"
    assert metadata.techniques == ["Tilt", "Double Lift"]
    assert metadata.psychological_principles == ["Time Misdirection"]
    assert metadata.performers == ["Example Performer"]
    assert {source.title for source in metadata.sources} == {
        "Card Notes",
        "Another Work",
    }
    assert metadata.extraction_method == "merged"
    assert metadata.confidence == 0.91
