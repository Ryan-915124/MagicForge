"""Legacy document parsing helpers; raw chunks are extraction context only."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from knowledge.chunking import ChunkingError, chunk_document
from knowledge.extractors import (
    DocumentExtractionError,
    ExtractedDocument,
    ExtractedSection,
    ExtractionRegistry,
    parse_front_matter,
)
from knowledge.metadata_extraction import (
    DeclaredMetadataExtractor,
    MetadataExtractor,
)
from knowledge.models import (
    ChunkMetadata,
    CognitiveMechanism,
    Effect,
    EntityType,
    KnowledgeChunk,
    KnowledgeEntity,
    KnowledgeMetadata,
    KnowledgeRelationship,
    Method,
    Performer,
    PsychologyPrinciple,
    RelationType,
    ResearchPaper,
    Source,
    SourceReference,
    Technique,
)


class IngestionError(ValueError):
    pass


_ENTITY_TYPE_ALIASES = {
    "effect": EntityType.EFFECT,
    "technique": EntityType.TECHNIQUE,
    "method": EntityType.METHOD,
    "psychology": EntityType.PSYCHOLOGY_PRINCIPLE,
    "psychology_principle": EntityType.PSYCHOLOGY_PRINCIPLE,
    "principle": EntityType.PSYCHOLOGY_PRINCIPLE,
    "performer": EntityType.PERFORMER,
    "source": EntityType.SOURCE,
    "cognitive_mechanism": EntityType.COGNITIVE_MECHANISM,
    "cognitive mechanism": EntityType.COGNITIVE_MECHANISM,
    "research_paper": EntityType.RESEARCH_PAPER,
    "research paper": EntityType.RESEARCH_PAPER,
}

_ENTITY_MODELS: dict[EntityType, type[KnowledgeEntity]] = {
    EntityType.EFFECT: Effect,
    EntityType.TECHNIQUE: Technique,
    EntityType.METHOD: Method,
    EntityType.PSYCHOLOGY_PRINCIPLE: PsychologyPrinciple,
    EntityType.PERFORMER: Performer,
    EntityType.SOURCE: Source,
    EntityType.COGNITIVE_MECHANISM: CognitiveMechanism,
    EntityType.RESEARCH_PAPER: ResearchPaper,
}

_KNOWN_DECLARED_METADATA = {
    "title",
    "author",
    "category",
    "technique",
    "psychology",
    "performer",
    "effect",
    "method",
    "sources",
    "tags",
    "entities",
    "relationships",
}


def load_document(
    path: str | Path,
    *,
    metadata_extractor: MetadataExtractor | None = None,
    extraction_registry: ExtractionRegistry | None = None,
    chunk_size: int = 1_200,
    chunk_overlap: int = 150,
) -> list[KnowledgeChunk]:
    """Parse a document into legacy temporary chunks; never write these to Qdrant."""

    registry = extraction_registry or ExtractionRegistry()
    extractor = metadata_extractor or DeclaredMetadataExtractor()
    try:
        document = registry.extract(path)
        raw_chunks = chunk_document(
            document,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
        base_metadata = _build_base_metadata(document)
    except (DocumentExtractionError, ChunkingError, ValueError) as exc:
        if isinstance(exc, IngestionError):
            raise
        raise IngestionError(str(exc)) from exc

    namespace = UUID(base_metadata.document_id)
    chunks = []
    for raw_chunk in raw_chunks:
        chunk_id = str(uuid5(namespace, f"{raw_chunk.chunk_index}:{raw_chunk.text}"))
        try:
            annotations = extractor.extract(
                raw_chunk.text, document, raw_chunk.source_locator
            )
        except Exception as exc:
            raise IngestionError(
                f"metadata extraction failed for {document.path} "
                f"chunk {raw_chunk.chunk_index}: {exc}"
            ) from exc
        metadata, linked_annotations = _enrich_chunk_metadata(
            base_metadata, annotations, chunk_id
        )
        chunks.append(
            KnowledgeChunk(
                id=chunk_id,
                text=raw_chunk.text,
                chunk_index=raw_chunk.chunk_index,
                heading=raw_chunk.heading,
                page_number=raw_chunk.page_number,
                source_locator=raw_chunk.source_locator,
                annotations=linked_annotations,
                metadata=metadata,
            )
        )
    return chunks


def load_markdown(
    path: str | Path,
    *,
    metadata_extractor: MetadataExtractor | None = None,
    chunk_size: int = 1_200,
    chunk_overlap: int = 150,
) -> list[KnowledgeChunk]:
    """Backward-compatible Markdown entrypoint."""

    markdown_path = Path(path)
    if markdown_path.suffix.casefold() not in {".md", ".markdown"}:
        raise IngestionError(f"unsupported file type: {markdown_path.suffix}")
    return load_document(
        markdown_path,
        metadata_extractor=metadata_extractor,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )


def parse_markdown(path: Path) -> tuple[dict[str, Any], str]:
    """Backward-compatible front-matter parser."""

    try:
        raw = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise IngestionError(f"could not read {path}: {exc}") from exc
    try:
        return parse_front_matter(raw, path)
    except DocumentExtractionError as exc:
        raise IngestionError(str(exc)) from exc


def build_metadata(
    path: Path, front_matter: Mapping[str, Any], markdown: str
) -> KnowledgeMetadata:
    """Backward-compatible document metadata builder."""

    title = str(front_matter.get("title") or _first_heading(markdown) or path.stem)
    document = ExtractedDocument(
        path=str(path),
        media_type="text/markdown",
        title=title,
        author=str(front_matter.get("author") or ""),
        sections=[ExtractedSection(text=markdown or title)],
        declared_metadata=dict(front_matter),
    )
    return _build_base_metadata(document)


def split_markdown(
    markdown: str,
    *,
    chunk_size: int = 1_200,
    chunk_overlap: int = 150,
) -> list[tuple[str | None, str]]:
    """Backward-compatible heading-aware chunk helper."""

    sections: list[ExtractedSection] = []
    heading: str | None = None
    lines: list[str] = []
    for line in markdown.splitlines():
        match = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
        if match:
            _append_legacy_section(sections, heading, lines)
            heading = match.group(1).strip()
            lines = []
        else:
            lines.append(line)
    _append_legacy_section(sections, heading, lines)
    if not sections:
        return []
    document = ExtractedDocument(
        path="memory.md",
        media_type="text/markdown",
        title="memory",
        sections=sections,
    )
    try:
        chunks = chunk_document(
            document, chunk_size=chunk_size, chunk_overlap=chunk_overlap
        )
    except ChunkingError as exc:
        raise IngestionError(str(exc)) from exc
    return [(chunk.heading, chunk.text) for chunk in chunks]


def _build_base_metadata(document: ExtractedDocument) -> KnowledgeMetadata:
    declared = document.declared_metadata
    document_id = str(
        uuid5(NAMESPACE_URL, f"magicforge:document:{Path(document.path).resolve()}")
    )
    source = Source(
        id=_source_id(document.title, document.author),
        name=document.title,
        attributes={
            "path": document.path,
            "author": document.author,
            "media_type": document.media_type,
        },
    )
    entities_by_id: dict[str, KnowledgeEntity] = {source.id: source}

    declared_entities = {
        EntityType.TECHNIQUE: _as_names(declared.get("technique")),
        EntityType.PSYCHOLOGY_PRINCIPLE: _as_names(declared.get("psychology")),
        EntityType.PERFORMER: _as_names(declared.get("performer")),
        EntityType.EFFECT: _as_names(declared.get("effect")),
        EntityType.METHOD: _as_names(declared.get("method")),
    }
    for entity_type, names in declared_entities.items():
        for name in names:
            entity = _make_entity(entity_type, name)
            entities_by_id[entity.id] = entity

    for source_reference in _source_references(declared.get("sources")):
        entity = Source(
            id=_source_id(source_reference.title, source_reference.author),
            name=source_reference.title,
            attributes={
                "author": source_reference.author,
                "locator": source_reference.locator,
            },
        )
        entities_by_id[entity.id] = entity

    explicit_entities = declared.get("entities") or []
    if not isinstance(explicit_entities, list):
        raise IngestionError("front matter `entities` must be a list")
    for raw_entity in explicit_entities:
        if not isinstance(raw_entity, Mapping):
            raise IngestionError("each entity must be a mapping")
        data = dict(raw_entity)
        entity_type = _entity_type(data.pop("type", None))
        try:
            entity = _ENTITY_MODELS[entity_type].model_validate(data)
        except Exception as exc:
            raise IngestionError(f"invalid entity {raw_entity!r}: {exc}") from exc
        entities_by_id[entity.id] = entity

    entities = list(entities_by_id.values())
    lookup = _entity_lookup(entities)
    relationships = _explicit_relationships(declared, lookup)

    attributes = {
        key: value
        for key, value in declared.items()
        if key not in _KNOWN_DECLARED_METADATA
    }
    attributes.update({"path": document.path, "media_type": document.media_type})
    return KnowledgeMetadata(
        document_id=document_id,
        source_id=source.id,
        title=document.title,
        author=document.author,
        category=str(declared.get("category") or ""),
        technique=declared_entities[EntityType.TECHNIQUE],
        psychology=declared_entities[EntityType.PSYCHOLOGY_PRINCIPLE],
        performer=declared_entities[EntityType.PERFORMER],
        entities=entities,
        relationships=relationships,
        tags=_as_names(declared.get("tags")),
        attributes=attributes,
    )


def _enrich_chunk_metadata(
    base: KnowledgeMetadata,
    annotations: ChunkMetadata,
    chunk_id: str,
) -> tuple[KnowledgeMetadata, ChunkMetadata]:
    entities_by_id = {entity.id: entity for entity in base.entities}
    for entity_type, names in (
        (EntityType.TECHNIQUE, annotations.techniques),
        (EntityType.PSYCHOLOGY_PRINCIPLE, annotations.psychological_principles),
        (EntityType.PERFORMER, annotations.performers),
    ):
        for name in names:
            entity = _make_entity(entity_type, name)
            entities_by_id[entity.id] = entity

    linked_sources = []
    for reference in annotations.sources:
        source_id = _source_id(reference.title, reference.author)
        if (
            reference.title.casefold() == base.title.casefold()
            and reference.author.casefold() == base.author.casefold()
        ):
            source_id = base.source_id or source_id
        source = Source(
            id=source_id,
            name=reference.title,
            attributes={
                "author": reference.author,
                "locator": reference.locator,
            },
        )
        entities_by_id[source.id] = source
        linked_sources.append(reference.model_copy(update={"source_id": source.id}))

    linked_annotations = annotations.model_copy(update={"sources": linked_sources})
    entities = list(entities_by_id.values())
    relationships = _add_document_relationships(
        list(base.relationships),
        entities,
        base.source_id,
        source_chunk_id=chunk_id,
    )
    relationships = _add_effect_relationships(relationships, entities, chunk_id)
    return (
        KnowledgeMetadata(
            document_id=base.document_id,
            source_id=base.source_id,
            title=base.title,
            author=base.author,
            category=linked_annotations.magic_category or base.category,
            technique=_unique([*base.technique, *linked_annotations.techniques]),
            psychology=_unique(
                [*base.psychology, *linked_annotations.psychological_principles]
            ),
            performer=_unique([*base.performer, *linked_annotations.performers]),
            entities=entities,
            relationships=relationships,
            tags=base.tags,
            attributes={
                **base.attributes,
                "metadata_extraction": linked_annotations.extraction_method,
                "metadata_confidence": linked_annotations.confidence,
            },
        ),
        linked_annotations,
    )


def _explicit_relationships(
    declared: Mapping[str, Any], lookup: Mapping[str, str]
) -> list[KnowledgeRelationship]:
    relationships = []
    raw_relationships = declared.get("relationships") or []
    if not isinstance(raw_relationships, list):
        raise IngestionError("front matter `relationships` must be a list")
    for raw in raw_relationships:
        if not isinstance(raw, Mapping):
            raise IngestionError("each relationship must be a mapping")
        data = dict(raw)
        source_ref = data.pop("source", data.pop("source_id", None))
        target_ref = data.pop("target", data.pop("target_id", None))
        data["source_id"] = _resolve_entity(source_ref, lookup, "source")
        data["target_id"] = _resolve_entity(target_ref, lookup, "target")
        try:
            relationships.append(KnowledgeRelationship.model_validate(data))
        except Exception as exc:
            raise IngestionError(f"invalid relationship {raw!r}: {exc}") from exc
    return relationships


def _add_document_relationships(
    relationships: list[KnowledgeRelationship],
    entities: list[KnowledgeEntity],
    source_id: str | None,
    source_chunk_id: str | None,
) -> list[KnowledgeRelationship]:
    if not source_id:
        return relationships
    existing = {(item.source_id, item.target_id, item.type) for item in relationships}
    for entity in entities:
        if entity.type == EntityType.SOURCE:
            if entity.id != source_id:
                key = (source_id, entity.id, RelationType.RELATED_TO)
                if key not in existing:
                    relationships.append(
                        KnowledgeRelationship(
                            source_id=source_id,
                            target_id=entity.id,
                            type=RelationType.RELATED_TO,
                            source_chunk_id=source_chunk_id,
                        )
                    )
                    existing.add(key)
            continue
        key = (source_id, entity.id, RelationType.EXPLAINS)
        if key not in existing:
            relationships.append(
                KnowledgeRelationship(
                    source_id=source_id,
                    target_id=entity.id,
                    type=RelationType.EXPLAINS,
                    source_chunk_id=source_chunk_id,
                )
            )
            existing.add(key)
    return relationships


def _add_effect_relationships(
    relationships: list[KnowledgeRelationship],
    entities: list[KnowledgeEntity],
    chunk_id: str,
) -> list[KnowledgeRelationship]:
    effects = [item for item in entities if item.type == EntityType.EFFECT]
    used = [
        item
        for item in entities
        if item.type
        in {
            EntityType.TECHNIQUE,
            EntityType.METHOD,
            EntityType.PSYCHOLOGY_PRINCIPLE,
        }
    ]
    performers = [item for item in entities if item.type == EntityType.PERFORMER]
    existing = {(item.source_id, item.target_id, item.type) for item in relationships}
    for effect in effects:
        for target, relation_type in [
            *((item, RelationType.USES) for item in used),
            *((item, RelationType.PERFORMED_BY) for item in performers),
        ]:
            key = (effect.id, target.id, relation_type)
            if key not in existing:
                relationships.append(
                    KnowledgeRelationship(
                        source_id=effect.id,
                        target_id=target.id,
                        type=relation_type,
                        source_chunk_id=chunk_id,
                    )
                )
                existing.add(key)
    return relationships


def _make_entity(entity_type: EntityType, name: str) -> KnowledgeEntity:
    return _ENTITY_MODELS[entity_type](name=name)


def _entity_type(value: Any) -> EntityType:
    key = str(value).strip().casefold().replace(" ", "_")
    try:
        return _ENTITY_TYPE_ALIASES[key]
    except KeyError as exc:
        allowed = ", ".join(sorted(_ENTITY_TYPE_ALIASES))
        raise IngestionError(
            f"unknown entity type {value!r}; expected one of {allowed}"
        ) from exc


def _entity_lookup(entities: list[KnowledgeEntity]) -> dict[str, str]:
    lookup = {}
    for entity in entities:
        lookup[entity.id] = entity.id
        lookup[entity.name.casefold()] = entity.id
    return lookup


def _resolve_entity(reference: Any, lookup: Mapping[str, str], endpoint: str) -> str:
    if reference is None:
        raise IngestionError(f"relationship {endpoint} is required")
    key = str(reference).strip()
    resolved = lookup.get(key) or lookup.get(key.casefold())
    if not resolved:
        raise IngestionError(
            f"relationship {endpoint} {reference!r} does not match an entity ID or name"
        )
    return resolved


def _source_id(title: str, author: str) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            f"magicforge:source:{author.casefold().strip()}:{title.casefold().strip()}",
        )
    )


def _source_references(value: Any) -> list[SourceReference]:
    if not value:
        return []
    if isinstance(value, (str, Mapping)):
        value = [value]
    output = []
    for item in value:
        if isinstance(item, str):
            output.append(SourceReference(title=item))
        elif isinstance(item, Mapping):
            output.append(SourceReference.model_validate(item))
    return output


def _as_names(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output = []
    for value in values:
        normalized = " ".join(value.split())
        key = normalized.casefold()
        if normalized and key not in seen:
            seen.add(key)
            output.append(normalized)
    return output


def _first_heading(markdown: str) -> str:
    match = re.search(r"^#\s+(.+?)\s*$", markdown, re.MULTILINE)
    return match.group(1).strip() if match else ""


def _append_legacy_section(
    sections: list[ExtractedSection], heading: str | None, lines: list[str]
) -> None:
    text = "\n".join(lines).strip()
    if text:
        sections.append(
            ExtractedSection(text=text, heading=heading, locator=heading or "document")
        )


def discover_documents(path: Path) -> list[Path]:
    registry = ExtractionRegistry()
    if path.is_file():
        return [path] if path.suffix.casefold() in registry.supported_suffixes else []
    return sorted(
        item
        for item in path.rglob("*")
        if item.is_file() and item.suffix.casefold() in registry.supported_suffixes
    )


def discover_markdown(path: Path) -> list[Path]:
    return [
        item
        for item in discover_documents(path)
        if item.suffix.casefold() in {".md", ".markdown"}
    ]


def ingest_paths(*_args: object, **_kwargs: object) -> int:
    """Removed unsafe compatibility entrypoint.

    Callers must use Source Approval -> Evidence Cards -> Storage Manifest.
    Keeping this explicit failure makes stale automation fail closed.
    """

    raise IngestionError(
        "raw document ingestion is disabled; create an authorized StorageManifest"
    )
