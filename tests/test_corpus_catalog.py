from aios_app.corpus_catalog import CorpusCatalogService, CorpusRoute, _domain_matches


def test_catalog_domain_matching_is_host_bounded():
    assert _domain_matches("digimon.fandom.com", "digimon.fandom.com")
    assert _domain_matches("wiki.example.org", ".example.org")
    assert _domain_matches("deep.wiki.example.org", "*.example.org")
    assert not _domain_matches("evil-example.org", ".example.org")
    assert not _domain_matches("example.org", ".example.org")


def test_catalog_fallback_is_safe_and_unclassified():
    route = CorpusCatalogService.FALLBACK
    assert route.collection_key == "inbox"
    assert route.scope_key == "unclassified"
    assert route.epistemic_namespace == "reference"
    assert route.identity_binding == "external"
    assert not route.classified


def test_explicit_catalog_route_keeps_acl_and_collection_separate():
    route = CorpusRoute(
        collection_key="digimon-fandom",
        scope_key="fiction.digimon",
        epistemic_namespace="canon.digimon",
        identity_binding="external",
        profile_key="digimon-fandom",
        matched_by="domain",
    )
    assert route.collection_key != route.scope_key
    assert route.scope_key == "fiction.digimon"
    assert route.identity_binding == "external"
