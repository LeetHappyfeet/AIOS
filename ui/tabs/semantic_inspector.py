"""Read-only semantic diagnostic tab; API calls keep model loading out of UI."""
from __future__ import annotations

import os
import requests
import gradio as gr
from aios_app.ui.registry import register_tab
from aios_app.ui.semantic_inspection_inputs import (
    api_error_detail, build_inspection_request, member_seed,
)


def _api(path: str, payload: dict | None = None):
    base = os.getenv("AIOS_API_URL", "http://127.0.0.1:8000").rstrip("/")
    try:
        response = (requests.post(base + path, json=payload, timeout=60) if payload is not None
                    else requests.get(base + path, timeout=30))
    except requests.RequestException as exc:
        raise ValueError(f"Cannot reach AIOS API: {exc}") from exc
    if not response.ok:
        try:
            body = response.json()
        except ValueError:
            body = None
        detail = api_error_detail(body, f"API returned HTTP {response.status_code}.")
        raise ValueError(detail)
    return response.json()


def inspect(collection, cluster, point_ids, neighbors, offset, evidence_offset, current_clusters=None):
    try:
        # IDs copied from the cluster list are cluster IDs, not vector IDs.
        listed = {str(item["cluster_id"]) for item in (current_clusters or {}).get("clusters", [])}
        if (point_ids or "").strip() in listed:
            cluster, point_ids, collection = point_ids.strip(), "", "propositions_v1"
        payload = build_inspection_request(collection, cluster, point_ids,
                                           int(neighbors), int(offset), int(evidence_offset))
        result = _api("/semantic/inspect", payload)
        if int(neighbors) and payload.get("cluster_id"):
            medoid = result["representatives"]["medoid_id"]
            members = result.get("members", [])
            index = next((i for i, item in enumerate(members) if str(item["proposition_id"]) == medoid), None)
            if index is None:
                raise ValueError("This cluster has no available representative vector. Choose another cluster.")
            seed, _, seed_collection, _ = member_seed(result, index)
            expanded = build_inspection_request(seed_collection, "", seed, int(neighbors), 0, int(evidence_offset))
            result = _api("/semantic/inspect", expanded)
            result["seed_selection"] = {"method": "cluster_sample_medoid", "point_id": seed,
                                        "cluster_id": payload["cluster_id"]}
    except (ValueError, TypeError, OverflowError, KeyError) as exc:
        return [], [], {"error": str(exc)}
    samples = result["representatives"]
    representative_ids = set(samples["representative_ids"])
    rows = []
    for member in result["members"]:
        key = member["proposition_id"]
        rows.append([key, "medoid" if key == samples["medoid_id"] else
                     "representative" if key in representative_ids else "",
                     member["canonical_text"], member["polarity"],
                     member.get("membership_kind"), len(member["evidence"])])
    edges = [[edge["text_a"], edge["text_b"], edge["similarity"],
              edge["relation"], edge["validation_status"], edge["boundary"]]
             for edge in result["edges"]]
    return rows, edges, result


def select_seed(result, evt: gr.SelectData):
    row_index = evt.index[0] if isinstance(evt.index, (tuple, list)) else evt.index
    try:
        return member_seed(result, row_index)
    except (ValueError, TypeError, KeyError) as exc:
        gr.Warning(str(exc))
        return gr.skip(), gr.skip(), gr.skip(), gr.skip()


def list_clusters():
    try:
        result = _api("/semantic/clusters")
        choices = [(f"{item['member_count']} members — {item.get('classification') or 'unclassified'} — {item['cluster_id']}",
                    item['cluster_id']) for item in result.get("clusters", [])]
        return result, gr.update(choices=choices, value=None)
    except ValueError as exc:
        return {"error": str(exc)}, gr.update(choices=[], value=None)


def choose_cluster(value):
    return value or "", "", "propositions_v1", 0


@register_tab
def render_semantic_inspector():
    with gr.Tab("Semantic neighborhoods"):
        gr.Markdown("Inspect a stored cluster or paste Qdrant point IDs. Source evidence, viewpoints, "
                    "frames and verifier explanations appear in the details. Choose a current cluster to inspect it. "
                    "With neighbors above 0, its medoid is selected automatically; click a member row to choose a different seed.")
        refresh = gr.Button("List current clusters")
        clusters = gr.JSON(label="Current clusters")
        cluster_picker = gr.Dropdown(label="Choose a current cluster", choices=[])
        refresh.click(list_clusters, outputs=[clusters, cluster_picker])
        collection = gr.Textbox(value="propositions_v1", label="Collection")
        cluster = gr.Textbox(label="Cluster ID (leave empty to inspect point IDs)")
        points = gr.Textbox(label="Point IDs / selected seed (comma or whitespace separated)")
        with gr.Row():
            neighbors = gr.Number(value=0, precision=0, label="Number of neighbors around selected seed")
            offset = gr.Number(value=0, precision=0, label="Member offset")
            evidence_offset = gr.Number(value=0, precision=0, label="Evidence offset per member")
        button = gr.Button("Inspect")
        members = gr.Dataframe(headers=["Proposition", "Representative", "Text", "Polarity", "Membership", "Evidence shown"], interactive=False)
        edges = gr.Dataframe(headers=["Text A", "Text B", "Similarity", "Relation", "Validation", "Boundary"], interactive=False)
        details = gr.JSON(label="Source evidence and relation explanations")
        cluster_picker.change(choose_cluster, inputs=cluster_picker, outputs=[cluster, points, collection, offset])
        members.select(select_seed, inputs=details, outputs=[points, cluster, collection, offset])
        button.click(inspect, inputs=[collection, cluster, points, neighbors, offset, evidence_offset, clusters],
                     outputs=[members, edges, details])
