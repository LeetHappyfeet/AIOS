from aios_app.corpus_adapters import AO3Adapter, GenericWebAdapter, classify_corpus_document


def test_ao3_adapter_uses_structured_tags_not_prose():
    result = classify_corpus_document(
        source_uri="https://archiveofourown.org/works/123",
        metadata={
            "title": "A story mentioning unrelated words",
            "ao3": {
                "fandoms": ["Digimon - All Media Types"],
                "characters": ["Renamon (Digimon)"],
                "relationships": ["Renamon & Original Character"],
                "tags": ["Adventure"],
            },
        },
    )
    values = {(facet.facet_type, facet.facet_value) for facet in result.facets}
    assert ("repository", "ao3") in values
    assert ("source_type", "fanfiction") in values
    assert ("canon_status", "fanwork") in values
    assert ("fandom", "digimon - all media types") in values
    assert ("character", "renamon (digimon)") in values
    assert result.epistemic_namespace == "fanwork"


def test_ao3_adapter_does_not_infer_character_from_document_text():
    result = classify_corpus_document(
        source_uri="https://archiveofourown.org/works/123",
        metadata={"title": "Renamon meets Shego"},
    )
    values = {(facet.facet_type, facet.facet_value) for facet in result.facets}
    assert not any(kind in {"fandom", "character"} for kind, _ in values)
    assert ("canon_status", "fanwork") in values


def test_generic_adapter_records_repository_without_claiming_subject():
    result = classify_corpus_document(
        source_uri="https://example.org/library/item",
        metadata={"title": "Renamon"},
    )
    values = {(facet.facet_type, facet.facet_value) for facet in result.facets}
    assert values == {("repository", "example.org")}
    assert result.epistemic_namespace is None


def test_ao3_html_extractor_reads_native_work_tags():
    from aios_app.accumulator.web.extractor import extract_structured_source_metadata

    html = """
    <dl class="work meta group">
      <dd class="fandom tags"><a class="tag">Digimon - All Media Types</a></dd>
      <dd class="character tags"><a class="tag">Renamon (Digimon)</a></dd>
      <dd class="relationship tags"><a class="tag">Renamon &amp; Rika Nonaka</a></dd>
      <dd class="freeform tags"><a class="tag">Adventure</a></dd>
    </dl>
    <div id="chapters">Shego appears in prose but is not tagged.</div>
    """
    data = extract_structured_source_metadata(
        html, "https://archiveofourown.org/works/123"
    )
    assert data["ao3"]["fandoms"] == ["Digimon - All Media Types"]
    assert data["ao3"]["characters"] == ["Renamon (Digimon)"]
    assert "Shego" not in repr(data)
