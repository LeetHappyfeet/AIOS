"""Read-only semantic diagnostic tab; API calls keep model loading out of UI."""
from __future__ import annotations

import os
import requests
import gradio as gr
from aios_app.ui.registry import register_tab


def _api(path: str, payload: dict | None = None):
    base = os.getenv("AIOS_API_URL", "http://127.0.0.1:8000").rstrip("/")
    response = (requests.post(base + path, json=payload, timeout=60) if payload is not None
                else requests.get(base + path, timeout=30))
    response.raise_for_status()
    return response.json()


def inspect(collection, cluster, point_ids, neighbors, offset, evidence_offset):
    payload = {"collection": collection, "neighbors": int(neighbors),
               "offset": int(offset), "evidence_offset": int(evidence_offset)}
    if cluster.strip():
        payload["cluster_id"] = cluster.strip()
    else:
        payload["point_ids"] = point_ids.replace(",", " ").split()
    result = _api("/semantic/inspect", payload)
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


@register_tab
def render_semantic_inspector():
    with gr.Tab("Semantic neighborhoods"):
        gr.Markdown("Inspect a stored cluster or paste Qdrant point IDs. Source evidence, viewpoints, "
                    "frames and verifier explanations appear in the details. Sampling uses original vectors.")
        refresh = gr.Button("List current clusters")
        clusters = gr.JSON(label="Current clusters")
        refresh.click(lambda: _api("/semantic/clusters"), outputs=clusters)
        collection = gr.Textbox(value="propositions_v1", label="Collection")
        cluster = gr.Textbox(label="Cluster ID (leave empty to inspect point IDs)")
        points = gr.Textbox(label="Point IDs (comma or whitespace separated)")
        with gr.Row():
            neighbors = gr.Number(value=0, precision=0, label="Neighbors around one seed")
            offset = gr.Number(value=0, precision=0, label="Member offset")
            evidence_offset = gr.Number(value=0, precision=0, label="Evidence offset per member")
        button = gr.Button("Inspect")
        members = gr.Dataframe(headers=["Proposition", "Representative", "Text", "Polarity", "Membership", "Evidence shown"], interactive=False)
        edges = gr.Dataframe(headers=["Text A", "Text B", "Similarity", "Relation", "Validation", "Boundary"], interactive=False)
        details = gr.JSON(label="Source evidence and relation explanations")
        button.click(inspect, inputs=[collection, cluster, points, neighbors, offset, evidence_offset],
                     outputs=[members, edges, details])
