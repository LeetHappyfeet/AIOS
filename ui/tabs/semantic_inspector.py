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


def _topology(path, **params):
    base = os.getenv("AIOS_API_URL", "http://127.0.0.1:8000").rstrip("/")
    try:
        response = requests.get(base + "/semantic/topology/" + path,
                                params=params, timeout=30)
    except requests.RequestException as exc:
        raise ValueError(f"Cannot reach AIOS API: {exc}") from exc
    if not response.ok:
        try:
            body = response.json()
        except ValueError:
            body = None
        raise ValueError(api_error_detail(body, f"HTTP {response.status_code}"))
    return response.json()


def overview():
    try:
        result = _topology("status")
        counts = result["counts"]
        run = result.get("latest_run")
        summary = (
            f"**{counts['propositions']}** propositions · "
            f"**{counts['indexed_propositions']}** indexed vectors · "
            f"**{counts['neighbor_pairs']}** candidate neighbor pairs · "
            f"**{counts['active_clusters']}** active clusters\n\n"
            f"{result['message']}\n\n"
            f"Active embedding: \`{result['embedding_version']}\`. "
            f"Latest region analysis: **{run['status']}** "
            f"({run['completed_at'] or 'not completed'})" if run else
            f"**{counts['propositions']}** propositions · "
            f"**{counts['indexed_propositions']}** indexed vectors · "
            f"**{counts['neighbor_pairs']}** candidate neighbor pairs · "
            f"**{counts['active_clusters']}** active clusters\n\n{result['message']}"
        )
        return summary, result
    except ValueError as exc:
        return f"**Topology status unavailable:** {exc}", {"error": str(exc)}


def search_propositions(query, offset, indexed_only):
    try:
        result = _topology("propositions", q=(query or "").strip(),
                           offset=max(0, int(offset or 0)),
                           indexed_only=str(bool(indexed_only)).lower())
        rows = [[str(item["proposition_id"]), item["canonical_text"],
                 item.get("topic_key") or "", bool(item.get("indexed"))]
                for item in result["results"]]
        message = (f"{len(rows)} results, starting at offset {result['offset']}. "
                   "Select a row to inspect its semantic neighborhood." if rows else
                   "No matching propositions. Try different text, or uncheck 'Indexed vectors only'.")
        return rows, message
    except (ValueError, TypeError) as exc:
        return [], f"Search unavailable: {exc}"


def choose_proposition(rows, evt: gr.SelectData):
    idx = evt.index[0] if isinstance(evt.index, (list, tuple)) else evt.index
    try:
        item = rows.iloc[idx].tolist() if hasattr(rows, "iloc") else rows[idx]
        if not item[3]:
            gr.Warning("This proposition has no active vector. It cannot be expanded in Qdrant.")
        return str(item[0]), "", "propositions_v1", 0
    except (IndexError, KeyError, TypeError, ValueError):
        gr.Warning("Select a proposition row.")
        return gr.skip(), gr.skip(), gr.skip(), gr.skip()


def list_outliers(offset):
    try:
        result = _topology("outliers", offset=max(0, int(offset or 0)))
        rows = [[str(item["proposition_id"]), item["canonical_text"],
                 item["reason"], item["nearest_similarity"]]
                for item in result["outliers"]]
        return rows, (f"{len(rows)} outlier candidates. Select one to inspect its neighbors." if rows else
                      "No current outlier candidates; inspect individual propositions instead.")
    except (ValueError, TypeError) as exc:
        return [], f"Outliers unavailable: {exc}"


def choose_outlier(rows, evt: gr.SelectData):
    idx = evt.index[0] if isinstance(evt.index, (list, tuple)) else evt.index
    try:
        item = rows.iloc[idx].tolist() if hasattr(rows, "iloc") else rows[idx]
        return str(item[0]), "", "propositions_v1", 0
    except (IndexError, KeyError, TypeError, ValueError):
        gr.Warning("Select an outlier row.")
        return gr.skip(), gr.skip(), gr.skip(), gr.skip()


@register_tab
def render_semantic_inspector():
    with gr.Tab("Semantic topology"):
        gr.Markdown(
            "Explore what AIOS actually knows and how its semantic propositions relate. "
            "Clusters are optional derived regions, **not** a prerequisite for inspecting vectors. "
            "All actions below are read-only; similarity is advisory rather than proof."
        )
        with gr.Row():
            refresh_status = gr.Button("Refresh topology status")
        status = gr.Markdown("Select **Refresh topology status** to inspect the active index.")
        raw_status = gr.JSON(label="Detailed pipeline status", visible=False)
        refresh_status.click(overview, outputs=[status, raw_status])

        with gr.Tabs():
            with gr.Tab("Find a proposition"):
                with gr.Row():
                    query = gr.Textbox(label="Search proposition text", placeholder="e.g. Renamon likes cooking", scale=5)
                    only_indexed = gr.Checkbox(value=True, label="Indexed vectors only")
                with gr.Row():
                    search_offset = gr.Number(value=0, precision=0, label="Results offset")
                    search_button = gr.Button("Search propositions", variant="primary")
                search_info = gr.Markdown("Search by words in a proposition, or leave the text blank to browse recent entries.")
                search_results = gr.Dataframe(
                    headers=["Proposition ID", "Text", "Topic", "Indexed"],
                    interactive=False, label="Select a row to inspect")
                search_button.click(search_propositions,
                    inputs=[query, search_offset, only_indexed],
                    outputs=[search_results, search_info])

            with gr.Tab("Clusters and outliers"):
                gr.Markdown(
                    "Clusters require coherent, sufficiently connected propositions. "
                    "An empty cluster list is normal when none qualify; outliers may still exist."
                )
                refresh = gr.Button("List current clusters")
                clusters = gr.JSON(label="Current clusters", visible=False)
                cluster_picker = gr.Dropdown(label="Choose a current cluster", choices=[])
                refresh.click(list_clusters, outputs=[clusters, cluster_picker])
                with gr.Row():
                    outlier_offset = gr.Number(value=0, precision=0, label="Outlier offset")
                    outlier_button = gr.Button("Browse current outliers")
                outlier_info = gr.Markdown()
                outlier_rows = gr.Dataframe(
                    headers=["Proposition ID", "Text", "Why unclustered", "Nearest similarity"],
                    interactive=False, label="Outliers (select to inspect)")
                outlier_button.click(list_outliers, inputs=outlier_offset,
                                     outputs=[outlier_rows, outlier_info])

            with gr.Tab("Advanced: inspect by ID"):
                gr.Markdown(
                    "For debugging: paste a Qdrant point UUID or a stored cluster UUID. "
                    "The proposition search above is usually easier."
                )
                collection = gr.Dropdown(
                    choices=["propositions_v1", "epistemic_objects_v1"],
                    value="propositions_v1", label="Vector collection")
                cluster = gr.Textbox(label="Cluster UUID (leave empty to inspect point IDs)")
                points = gr.Textbox(label="Qdrant point UUIDs / selected seed")

        gr.Markdown("### Neighborhood inspection")
        gr.Markdown(
            "Selecting a proposition, outlier, or cluster above fills the ID fields under "
            "**Advanced: inspect by ID**. Set neighbors above zero to explore similar vectors, "
            "then click **Inspect**. Click a member row to change the seed."
        )
        with gr.Row():
            neighbors = gr.Number(value=12, precision=0, label="Neighbors around selected seed")
            offset = gr.Number(value=0, precision=0, label="Member offset")
            evidence_offset = gr.Number(value=0, precision=0, label="Evidence offset per member")
        button = gr.Button("Inspect", variant="primary")
        members = gr.Dataframe(
            headers=["Proposition", "Representative", "Text", "Polarity", "Membership", "Evidence shown"],
            interactive=False, label="Members (click to select the next seed)")
        edges = gr.Dataframe(
            headers=["Text A", "Text B", "Similarity", "Relation", "Validation", "Boundary"],
            interactive=False, label="Candidate semantic relationships")
        details = gr.JSON(label="Source evidence, frames and relationship explanations")

        cluster_picker.change(choose_cluster, inputs=cluster_picker,
                              outputs=[cluster, points, collection, offset])
        search_results.select(choose_proposition, inputs=search_results,
                              outputs=[points, cluster, collection, offset])
        outlier_rows.select(choose_outlier, inputs=outlier_rows,
                            outputs=[points, cluster, collection, offset])
        members.select(select_seed, inputs=details, outputs=[points, cluster, collection, offset])
        button.click(inspect, inputs=[collection, cluster, points, neighbors, offset,
                                      evidence_offset, clusters],
                     outputs=[members, edges, details])
