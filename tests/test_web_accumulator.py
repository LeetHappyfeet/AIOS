from pathlib import Path

from aios_app.accumulator.ingest.jsonl_ingestor import JSONLDAGIngestor
from aios_app.accumulator.web.extractor import extract_links, extract_page_metadata
from aios_app.accumulator.web.queue import CrawlQueue, CrawlTask


def test_crawl_task_defaults_to_single_page_and_source_identity():
    task = CrawlTask(
        url="https://example.com/article",
        source_id="example.com",
    )
    assert task.crawl_mode == "page"
    assert task.max_pages == 1
    assert task.source_id == "example.com"
    assert task.target_character_id is None
    assert task.target_world_id is None


def test_queue_tracks_status_without_losing_provenance():
    queue = CrawlQueue()
    task = CrawlTask(
        url="https://example.com/article",
        source_id="example",
        source_kind="website",
    )
    task_id = queue.add(task)
    status = queue.get(task_id)

    assert status is not None
    assert status["source_id"] == "example"
    assert status["state"] == "queued"

    queue.update(task_id, state="running", pages_written=2)
    updated = queue.get(task_id)
    assert updated["state"] == "running"
    assert updated["pages_written"] == 2


def test_page_metadata_preserves_evidence_bearing_fields():
    html = """
    <html>
      <head>
        <title>Fallback title</title>
        <meta property="og:title" content="Observed title">
        <meta name="author" content="Jane Reporter">
        <meta property="article:published_time" content="2026-09-06T12:00:00Z">
        <meta property="og:site_name" content="Example News">
        <link rel="canonical" href="/canonical-story">
      </head>
      <body><article>Body</article></body>
    </html>
    """
    meta = extract_page_metadata(html, "https://example.com/story")
    assert meta["title"] == "Observed title"
    assert meta["author"] == "Jane Reporter"
    assert meta["published_at"] == "2026-09-06T12:00:00Z"
    assert meta["site_name"] == "Example News"
    assert meta["canonical_url"] == "https://example.com/canonical-story"


def test_link_extraction_normalizes_and_deduplicates():
    html = """
    <a href="/one#fragment">One</a>
    <a href="https://example.com/one">Duplicate</a>
    <a href="https://example.com/two">Two</a>
    <a href="mailto:test@example.com">Mail</a>
    """
    links = extract_links(html, "https://example.com/root")
    assert links == [
        "https://example.com/one",
        "https://example.com/two",
    ]


def test_v1_web_record_is_mapped_to_source_not_fake_character():
    ingestor = JSONLDAGIngestor(None, Path("."))
    context = ingestor._source_context(
        {
            "url": "https://news.example.com/story",
            "content": {"text": "Observed text."},
        }
    )
    assert context["source_id"] == "news.example.com"
    assert context["source_kind"] == "website"
    assert context["speaker_id"] is None
    assert context["target_character_id"] is None


def test_v2_web_record_keeps_source_speaker_and_targets_separate():
    ingestor = JSONLDAGIngestor(None, Path("."))
    context = ingestor._source_context(
        {
            "url": "https://example.com/story",
            "source": {
                "source_id": "example_news",
                "source_kind": "news_organization",
                "source_name": "Example News",
                "speaker_id": "example_news",
            },
            "target": {
                "character_id": "alice",
                "world_id": "00000000-0000-0000-0000-000000000001",
            },
            "content": {"text": "Observed text."},
        }
    )
    assert context["source_id"] == "example_news"
    assert context["speaker_id"] == "example_news"
    assert context["target_character_id"] == "alice"
    assert context["target_world_id"] == "00000000-0000-0000-0000-000000000001"


def test_queue_recovers_pending_tasks(tmp_path):
    state_path = tmp_path / "crawl-state.json"
    queue = CrawlQueue(state_path)
    task = CrawlTask(
        url="https://example.com/article",
        source_id="example",
    )
    task_id = queue.add(task)
    queue.update(task_id, state="running", current_url=task.url)

    recovered = CrawlQueue(state_path)
    status = recovered.get(task_id)

    assert status is not None
    assert status["state"] == "queued"
    assert "Recovered" in status["message"]
    popped = recovered.pop()
    assert popped is not None
    assert popped.task_id == task_id
