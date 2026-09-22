from aios_app.corpus_catalog import CorpusCatalogService, CorpusRoute, _domain_matches, _path_matches


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


def test_corpus_crawl_defaults_to_same_domain():
    from aios_app.accumulator.web.queue import CrawlTask
    task = CrawlTask(url="https://example.org/wiki/A", source_id="example.org", crawl_mode="site")
    assert task.same_domain_only is True


def test_catalog_path_matching_is_prefix_bounded():
    assert _path_matches("https://example.org/wiki/Shego", "/wiki")
    assert _path_matches("https://example.org/wiki/Shego/History", "/wiki")
    assert not _path_matches("https://example.org/post/get-to-know-shego", "/wiki")
    assert not _path_matches("https://example.org/wikievil/Shego", "/wiki")


def test_catalog_path_is_optional():
    assert _path_matches("https://example.org/anything", None)


def test_source_profile_access_class_defaults_restricted():
    from aios_app.models import CorpusSourceProfileIn
    profile = CorpusSourceProfileIn(
        profile_key="private-reference",
        collection_key="private-reference",
        scope_key="private.reference",
        domain_pattern="example.org",
    )
    assert profile.access_class == "restricted"


def test_source_profile_can_declare_public_reference():
    from aios_app.models import CorpusSourceProfileIn
    profile = CorpusSourceProfileIn(
        profile_key="public-reference",
        collection_key="public-reference",
        scope_key="reference.public",
        domain_pattern="reference.example.org",
        access_class="public",
    )
    assert profile.access_class == "public"
