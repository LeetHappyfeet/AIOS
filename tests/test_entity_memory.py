"""Regression examples from real extraction output; no trained model required."""
import json
from uuid import uuid4

import pytest
import spacy
from spacy.tokens import Doc, Span
from rdflib import Dataset, URIRef

from aios_app.entity_memory.extraction import anchor_id, extract_memory
from aios_app.entity_memory.service import cached_doc, rdf_update, graph_iri


def scene_doc(label="ORG", name="Rogue"):
    doc = Doc(spacy.blank("en").vocab,
        words=f"{name} owns a blue mug . The mug is in the kitchen .".split(),
        heads=[1,1,4,4,1,1,7,8,8,8,11,9,8],
        deps=["nsubj","ROOT","det","amod","dobj","punct","det","nsubj","ROOT","prep","det","pobj","punct"],
        pos=["PROPN","VERB","DET","ADJ","NOUN","PUNCT","DET","NOUN","AUX","ADP","DET","NOUN","PUNCT"],
        lemmas=f"{name} own a blue mug . the mug be in the kitchen .".split())
    doc.ents = [Span(doc,0,1,label=label)]
    return doc


def extract(doc=None, **kw):
    return extract_memory(doc if doc is not None else scene_doc(), scope="char:R:world:W:timeline:T",
                          section_id="source", **kw)


def test_known_identity_overrides_bad_ner_and_type_is_not_identity():
    a = extract(identities={"rogue":{"Rogue_001"}})
    b = extract(scene_doc("PERSON"), identities={"rogue":{"Rogue_001"}})
    rogue = a["anchors"][0]
    assert rogue["kind"] == "person"
    assert rogue["entity_id"] == b["anchors"][0]["entity_id"]
    assert a["mentions"][0]["ner_label"] == "ORG"
    assert rogue["character_id"] == "Rogue_001"


def test_explicit_transport_participant_can_anchor_an_unregistered_person():
    a = extract(participants={"rogue":"Rogue"})
    assert a["anchors"][0]["kind"] == "person"
    assert a["anchors"][0]["character_id"] is None
    assert a["anchors"][0]["participant_id"] == "Rogue"


def test_same_source_definite_reference_links_but_new_indefinite_does_not():
    result = extract()
    assert result["mentions"][1]["entity_id"] == result["mentions"][2]["entity_id"]
    assert len(result["relations"]) == 2
    assert {r["predicate"] for r in result["relations"]} == {"possesses","located_in"}
    doc = Doc(spacy.blank("en").vocab, words="a mug and a mug".split(),
              heads=[1,1,1,4,1], deps=["det","ROOT","cc","det","conj"],
              pos=["DET","NOUN","CCONJ","DET","NOUN"], lemmas="a mug and a mug".split())
    result = extract(doc)
    assert len({m["entity_id"] for m in result["mentions"]}) == 2


def test_ambiguous_identity_stays_unlinked():
    result = extract(identities={"rogue":{"one","two"}})
    assert result["mentions"][0]["reason"] == "ambiguous_identity"
    assert not result["mentions"][0]["entity_id"]
    assert not any(r["predicate"] == "possesses" for r in result["relations"])


@pytest.mark.parametrize("word,pos,lemma", [
    ("*","SYM","*"),("Damn!","INTJ","damn"),("pathetic","ADJ","pathetic"),
    ("moment","NOUN","moment"),("distance","NOUN","distance"),
    ("shoulders","NOUN","shoulder"),("she","PRON","she"),
])
def test_noise_attributes_body_parts_and_pronouns_never_become_anchors(word,pos,lemma):
    doc = Doc(spacy.blank("en").vocab, words=[word], heads=[0], deps=["ROOT"], pos=[pos], lemmas=[lemma])
    assert extract(doc)["anchors"] == []


def test_scope_and_source_separate_entities():
    assert anchor_id("timeline A","character:Rogue") != anchor_id("timeline B","character:Rogue")
    a = extract()
    b = extract_memory(scene_doc(),scope="char:R:world:W:timeline:T",section_id="another")
    assert a["anchors"][0]["entity_id"] != b["anchors"][0]["entity_id"]


def test_cached_json_roundtrip_does_not_run_a_model(monkeypatch):
    doc = scene_doc()
    def forbidden(*a, **kw): raise AssertionError("must not run NLP inference")
    monkeypatch.setattr(spacy, "load", forbidden)
    restored = cached_doc(json.dumps(doc.to_json()))
    assert extract(restored) == extract(doc)


def test_rdf_is_valid_source_qualified_replaces_stale_data_and_escapes_literals():
    section = str(uuid4())
    payload = extract(identities={"rogue":{"Rogue_001"}})
    payload.update(scope="private",owner="Rogue",contexts=[],section_id=section)
    payload["anchors"][0]["label"] = 'Rogue "quoted" \\ name\nline 🦊'
    dataset = Dataset()
    dataset.update(rdf_update(section,payload))
    graph = dataset.graph(URIRef(graph_iri(section)))
    assert len(graph) > 0
    ns = "urn:aios:entity-memory#"
    evidence = list(graph.subjects(URIRef(ns+"predicate"),None))
    assert len(evidence) == 2
    for r in evidence:
        for field in ("status","scope","polarity","source_text","predicate_offset","modal","attribution"):
            assert list(graph.objects(r,URIRef(ns+field)))
    assert not list(graph.triples((None,URIRef(ns+"possesses"),None)))
    size = len(graph)
    dataset.update(rdf_update(section,payload))
    assert len(graph) == size
    dataset.update(rdf_update(section,{}))
    assert len(graph) == 0


def test_ambiguous_definite_description_is_not_a_third_object():
    nlp = pytest.importorskip("en_core_web_sm").load()
    result = extract(nlp("Rogue owns a mug and a mug. The mug is in the kitchen."))
    mention = next(m for m in result["mentions"] if m["text"] == "The mug")
    assert mention["entity_id"] is None
    assert mention["reason"] == "ambiguous_local_reference"
    assert not any(r["predicate"] == "located_in" for r in result["relations"])


@pytest.mark.parametrize("text,polarity,scope,modal,attribution", [
    ("Rogue does not own a mug.",-1,"asserted",None,None),
    ("If Rogue owns a mug, Renamon might have a bowl.",1,"conditional","might",None),
    ("Mia says Rogue owns a mug.",1,"asserted",None,"say"),
])
def test_model_parse_preserves_negation_conditionals_modals_and_attribution(text,polarity,scope,modal,attribution):
    nlp = pytest.importorskip("en_core_web_sm").load()
    result=extract(nlp(text),identities={"rogue":{"Rogue"},"renamon":{"Renamon"}})
    relation=result["relations"][0]
    assert relation["polarity"]==polarity
    assert relation["scope"]==scope
    if modal: assert any(modal in r["modal"] for r in result["relations"])
    if attribution: assert any(a["predicate"]==attribution for a in relation["attribution"])


def test_negative_belief_does_not_invert_the_embedded_relationship():
    nlp=pytest.importorskip("en_core_web_sm").load()
    result=extract(nlp("Mia does not believe Rogue owns a mug."))
    relation=result["relations"][0]
    assert relation["polarity"]==1
    assert any(a["predicate"]=="believe" and a["polarity"]==-1 for a in relation["attribution"])


def test_negative_determiner_is_preserved_as_negative_possession_evidence():
    nlp=pytest.importorskip("en_core_web_sm").load()
    result=extract(nlp("Rogue owns no mugs."))
    assert result["relations"][0]["polarity"]==-1
