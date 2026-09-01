#!/usr/bin/env python3
"""Harvest JSON-LD from a DTS API and merge into one Turtle file."""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from typing import Any

import requests
from dts_validator.client import DTS_API
from rdflib import Graph
from tqdm.auto import tqdm
from uritemplate import URITemplate

LOGGER = logging.getLogger("dts_harvest")
DTS_CONTEXT_URL = "https://dtsapi.org/context/v1.0.json"


@dataclass
class HarvestStats:
    visited_collections: int = 0
    visited_resources: int = 0
    visited_citation_trees: int = 0
    visited_citable_units: int = 0
    navigation_requests: int = 0
    jsonld_objects_seen: int = 0
    jsonld_objects_deduplicated: int = 0
    jsonld_objects_parsed: int = 0
    jsonld_objects_skipped: int = 0


def configure_logging(level: str, log_file: str) -> None:
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    file_handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    )
    root_logger.addHandler(file_handler)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Harvest JSON-LD from DTS and merge into Turtle."
    )
    parser.add_argument(
        "--entrypoint",
        default="http://rs4.ethz.ch/dts/",
        help="DTS entry endpoint URL.",
    )
    parser.add_argument(
        "--output",
        default="data/harvest/merged.ttl",
        help="Path to output Turtle file.",
    )
    parser.add_argument(
        "--max-resources",
        type=int,
        default=None,
        help="Optional safety cap for visited resources.",
    )
    parser.add_argument(
        "--collection-id",
        default=None,
        help="If provided, crawl only this collection subtree.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level.",
    )
    parser.add_argument(
        "--log-file",
        default="data/logs/harvest.log",
        help="Path to log file (logging is written here, not stdout).",
    )
    return parser


def has_jsonld_markers(node: dict[str, Any]) -> bool:
    return "@context" in node or "@id" in node or "@type" in node


def normalize_prefixed_object(
    node: dict[str, Any],
    key: str,
    namespace_uri: str,
) -> dict[str, Any]:
    normalized = dict(node)
    prefixed_object = normalized.get(key)
    if not isinstance(prefixed_object, dict):
        return normalized

    # DTS exposes dublinCore as an object (e.g., {"creator": [...]}).
    # Expand to absolute IRIs so rdflib resolves to real DC Terms predicates.
    for child_key, child_value in prefixed_object.items():
        normalized[f"{namespace_uri}{child_key}"] = child_value
    del normalized[key]
    return normalized


def ensure_context(node: dict[str, Any], default_context_url: str) -> dict[str, Any]:
    normalized = dict(node)
    if "@context" not in normalized:
        normalized["@context"] = default_context_url
    return normalized


def try_parse_jsonld(graph: Graph, candidate: dict[str, Any]) -> bool:
    try:
        graph.parse(data=json.dumps(candidate), format="json-ld")
        return True
    except Exception as exc:  # pylint: disable=broad-except
        LOGGER.debug("Skipping non-parseable JSON-LD object: %s", exc)
        return False


def add_jsonld_from_object(
    obj: Any,
    graph: Graph,
    stats: HarvestStats,
    seen_jsonld_signatures: set[str],
    seen_resource_citation_tree_ids: dict[str, set[str]],
) -> None:
    if not isinstance(obj, dict) or not has_jsonld_markers(obj):
        return

    candidate = normalize_prefixed_object(
        obj,
        key="dublinCore",
        namespace_uri="http://purl.org/dc/terms/",
    )
    candidate = ensure_context(candidate, default_context_url=DTS_CONTEXT_URL)
    candidate = _deduplicate_citation_trees_deep(candidate)
    candidate = normalize_endpoint_links(candidate)
    candidate = _deduplicate_citation_trees_by_resource_id(
        candidate,
        seen_resource_citation_tree_ids,
    )
    stats.jsonld_objects_seen += 1
    candidate_signature = json.dumps(
        candidate,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    if candidate_signature in seen_jsonld_signatures:
        stats.jsonld_objects_deduplicated += 1
        return
    seen_jsonld_signatures.add(candidate_signature)
    if try_parse_jsonld(graph, candidate):
        stats.jsonld_objects_parsed += 1
    else:
        stats.jsonld_objects_skipped += 1


def safe_object_id(obj: Any) -> str | None:
    if isinstance(obj, dict):
        obj_id = obj.get("@id") or obj.get("id")
        return str(obj_id) if obj_id else None
    return None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _extract_citation_trees(resource_json: dict[str, Any]) -> list[dict[str, Any]]:
    raw_trees = _as_list(resource_json.get("citationTrees"))
    trees: list[dict[str, Any]] = []
    for tree in raw_trees:
        if isinstance(tree, dict):
            trees.append(tree)
    return trees


def _citation_tree_signature(tree: dict[str, Any]) -> str:
    try:
        return json.dumps(tree, sort_keys=True)
    except TypeError:
        return str(tree)


def _deduplicate_resource_citation_trees(resource_json: dict[str, Any]) -> dict[str, Any]:
    deduped = dict(resource_json)
    trees = _extract_citation_trees(deduped)
    if not trees:
        return deduped

    unique_trees: list[dict[str, Any]] = []
    seen_tree_keys: set[str] = set()
    for tree in trees:
        identifier = tree.get("identifier")
        if isinstance(identifier, str) and identifier.strip():
            tree_key = f"identifier:{identifier.strip()}"
        else:
            tree_key = f"signature:{_citation_tree_signature(tree)}"
        if tree_key in seen_tree_keys:
            continue
        seen_tree_keys.add(tree_key)
        unique_trees.append(tree)

    deduped["citationTrees"] = unique_trees
    return deduped


def _deduplicate_citation_trees_deep(value: Any) -> Any:
    if isinstance(value, dict):
        normalized = {key: _deduplicate_citation_trees_deep(val) for key, val in value.items()}
        if "citationTrees" in normalized:
            normalized = _deduplicate_resource_citation_trees(normalized)
        return normalized
    if isinstance(value, list):
        return [_deduplicate_citation_trees_deep(item) for item in value]
    return value


def _deduplicate_citation_trees_by_resource_id(
    value: Any,
    seen_resource_citation_tree_ids: dict[str, set[str]],
) -> Any:
    if isinstance(value, dict):
        normalized = {
            key: _deduplicate_citation_trees_by_resource_id(val, seen_resource_citation_tree_ids)
            for key, val in value.items()
        }
        object_id = safe_object_id(normalized)
        if object_id and "citationTrees" in normalized:
            unique_trees: list[dict[str, Any]] = []
            seen_for_resource = seen_resource_citation_tree_ids.setdefault(object_id, set())
            for tree in _extract_citation_trees(normalized):
                identifier = tree.get("identifier")
                if isinstance(identifier, str) and identifier.strip():
                    tree_key = f"identifier:{identifier.strip()}"
                else:
                    tree_key = f"signature:{_citation_tree_signature(tree)}"
                if tree_key in seen_for_resource:
                    continue
                seen_for_resource.add(tree_key)
                unique_trees.append(tree)
            if unique_trees:
                normalized["citationTrees"] = unique_trees
            else:
                normalized.pop("citationTrees", None)
        return normalized
    if isinstance(value, list):
        return [
            _deduplicate_citation_trees_by_resource_id(item, seen_resource_citation_tree_ids)
            for item in value
        ]
    return value


def _resource_obj_id(resource: Any) -> str:
    return safe_object_id(getattr(resource, "json", {})) or str(getattr(resource, "id", ""))


def _is_uri_template(value: str) -> bool:
    return "{" in value or "}" in value


def expand_endpoint(template: str, **params: Any) -> str:
    filtered = {key: value for key, value in params.items() if value is not None}
    return URITemplate(template).expand(filtered)


def _tree_identifier(tree: dict[str, Any]) -> str | None:
    identifier = tree.get("identifier")
    if isinstance(identifier, str) and identifier.strip():
        return identifier.strip()
    return None


def _endpoint_node(uri: str) -> dict[str, str]:
    return {"@id": uri}


def _normalize_object_endpoints(node: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(node)
    object_id = safe_object_id(normalized)
    if not object_id:
        return normalized

    collection_template = normalized.get("collection")
    if isinstance(collection_template, str) and _is_uri_template(collection_template):
        normalized["collection"] = _endpoint_node(
            expand_endpoint(collection_template, id=object_id)
        )

    document_template = normalized.get("document")
    if isinstance(document_template, str) and _is_uri_template(document_template):
        normalized["document"] = _endpoint_node(
            expand_endpoint(document_template, resource=object_id)
        )

    navigation_value = normalized.get("navigation")
    if isinstance(navigation_value, str) and _is_uri_template(navigation_value):
        trees = _extract_citation_trees(normalized)
        if str(normalized.get("@type", "")) == "Resource" and trees:
            navigation_links = [
                _endpoint_node(
                    expand_endpoint(
                        navigation_value,
                        resource=object_id,
                        tree=_tree_identifier(tree),
                        down=-1,
                    )
                )
                for tree in trees
            ]
            normalized["navigation"] = navigation_links
        else:
            normalized["navigation"] = _endpoint_node(
                expand_endpoint(navigation_value, resource=object_id, down=-1)
            )

    return normalized


def normalize_endpoint_links(value: Any) -> Any:
    if isinstance(value, dict):
        normalized = {key: normalize_endpoint_links(val) for key, val in value.items()}
        if normalized.get("@type") in {"Resource", "Collection"} or any(
            key in normalized for key in ("collection", "document", "navigation")
        ):
            normalized = _normalize_object_endpoints(normalized)
        return normalized
    if isinstance(value, list):
        return [normalize_endpoint_links(item) for item in value]
    return value


def navigation_uri_for_tree(resource_json: dict[str, Any], tree: dict[str, Any]) -> str | None:
    navigation_value = resource_json.get("navigation")
    if not isinstance(navigation_value, str) or not _is_uri_template(navigation_value):
        return None
    object_id = safe_object_id(resource_json)
    if not object_id:
        return None
    return expand_endpoint(
        navigation_value,
        resource=object_id,
        tree=_tree_identifier(tree),
        down=-1,
    )


def _crawl_citable_units_for_tree(
    dts_client: DTS_API,
    resource: Any,
    tree: dict[str, Any],
    graph: Graph,
    stats: HarvestStats,
    seen_citable_unit_ids: set[str],
    seen_jsonld_signatures: set[str],
    seen_resource_citation_tree_ids: dict[str, set[str]],
) -> None:
    resource_json = getattr(resource, "json", {})
    tree_id = _tree_identifier(tree) or "<unknown>"
    navigation_uri = navigation_uri_for_tree(resource_json, tree)
    if navigation_uri is None:
        LOGGER.debug(
            "Missing navigation URI template for resource %s (tree %s)",
            _resource_obj_id(resource),
            tree_id,
        )
        return

    LOGGER.debug("Fetching navigation for tree %s: %s", tree_id, navigation_uri)
    stats.navigation_requests += 1
    response = requests.get(navigation_uri, timeout=60)
    if response.status_code != 200:
        LOGGER.debug(
            "CitationTree navigation request failed for resource %s (tree %s): %s",
            _resource_obj_id(resource),
            tree_id,
            response.status_code,
        )
        return

    navigation_json = response.json()
    add_jsonld_from_object(
        navigation_json,
        graph,
        stats,
        seen_jsonld_signatures,
        seen_resource_citation_tree_ids,
    )
    for unit in _as_list(navigation_json.get("member")):
        if not isinstance(unit, dict):
            continue
        unit_id = str(unit.get("identifier") or "")
        if not unit_id:
            continue
        unit_key = f"{_resource_obj_id(resource)}::{unit_id}"
        if unit_key not in seen_citable_unit_ids:
            seen_citable_unit_ids.add(unit_key)
            stats.visited_citable_units += 1


def _process_resource(
    dts_client: DTS_API,
    resource: Any,
    graph: Graph,
    stats: HarvestStats,
    seen_resource_ids: set[str],
    seen_citation_tree_signatures: set[tuple[str, str]],
    seen_citable_unit_ids: set[str],
    seen_jsonld_signatures: set[str],
    seen_resource_citation_tree_ids: dict[str, set[str]],
    max_resources: int | None,
    pbar: tqdm,
) -> bool:
    resource_id = _resource_obj_id(resource)
    if resource_id in seen_resource_ids:
        return True
    if max_resources is not None and stats.visited_resources >= max_resources:
        LOGGER.warning("Reached max resources cap (%s); stopping crawl.", max_resources)
        return False

    seen_resource_ids.add(resource_id)
    stats.visited_resources += 1
    add_jsonld_from_object(
        getattr(resource, "json", {}),
        graph,
        stats,
        seen_jsonld_signatures,
        seen_resource_citation_tree_ids,
    )
    if pbar.total is not None:
        pbar.total += 1
    pbar.update(1)
    pbar.set_postfix(
        collections=stats.visited_collections,
        resources=stats.visited_resources,
        citation_trees=stats.visited_citation_trees,
        citable_units=stats.visited_citable_units,
    )

    try:
        resource_collection = dts_client.collections(id=resource.id)
        add_jsonld_from_object(
            getattr(resource_collection, "json", {}),
            graph,
            stats,
            seen_jsonld_signatures,
            seen_resource_citation_tree_ids,
        )
    except Exception as exc:  # pylint: disable=broad-except
        LOGGER.debug("Resource collection metadata unavailable for %s: %s", resource.id, exc)

    resource_json = getattr(resource, "json", {})
    citation_trees = _extract_citation_trees(resource_json)
    for tree in citation_trees:
        signature = _citation_tree_signature(tree)
        dedupe_key = (resource_id, signature)
        if dedupe_key in seen_citation_tree_signatures:
            continue
        seen_citation_tree_signatures.add(dedupe_key)
        stats.visited_citation_trees += 1
        _crawl_citable_units_for_tree(
            dts_client=dts_client,
            resource=resource,
            tree=tree,
            graph=graph,
            stats=stats,
            seen_citable_unit_ids=seen_citable_unit_ids,
            seen_jsonld_signatures=seen_jsonld_signatures,
            seen_resource_citation_tree_ids=seen_resource_citation_tree_ids,
        )

    pbar.set_postfix(
        collections=stats.visited_collections,
        resources=stats.visited_resources,
        citation_trees=stats.visited_citation_trees,
        citable_units=stats.visited_citable_units,
    )
    return True


def crawl(
    dts_client: DTS_API,
    graph: Graph,
    stats: HarvestStats,
    max_resources: int | None,
    collection_id: str | None,
) -> None:
    visited_collection_ids: set[str] = set()
    visited_resource_ids: set[str] = set()
    visited_citation_tree_signatures: set[tuple[str, str]] = set()
    visited_citable_unit_ids: set[str] = set()
    seen_jsonld_signatures: set[str] = set()
    seen_resource_citation_tree_ids: dict[str, set[str]] = {}

    if collection_id:
        LOGGER.info("Limiting crawl to collection id: %s", collection_id)
        root_collection = dts_client.collections(id=collection_id)
        queue = [root_collection]
    else:
        collections = dts_client.collections(recursive=False)
        queue = [collection for collection in collections]
    queued_collection_ids: set[str] = {
        safe_object_id(getattr(collection, "json", {})) or str(getattr(collection, "id", ""))
        for collection in queue
    }

    pbar = tqdm(
        total=len(queue),
        desc="Crawling DTS",
        unit="obj",
    )

    try:
        while queue:
            collection = queue.pop(0)
            collection_id = safe_object_id(getattr(collection, "json", {})) or str(
                getattr(collection, "id", "")
            )
            queued_collection_ids.discard(collection_id)
            if collection_id in visited_collection_ids:
                continue

            visited_collection_ids.add(collection_id)
            stats.visited_collections += 1
            add_jsonld_from_object(
                getattr(collection, "json", {}),
                graph,
                stats,
                seen_jsonld_signatures,
                seen_resource_citation_tree_ids,
            )
            pbar.update(1)
            pbar.set_postfix(
                collections=stats.visited_collections,
                resources=stats.visited_resources,
                citation_trees=stats.visited_citation_trees,
                citable_units=stats.visited_citable_units,
            )

            try:
                collection_details = dts_client.collections(id=collection.id)
            except Exception as exc:  # pylint: disable=broad-except
                LOGGER.warning("Failed loading collection %s: %s", collection.id, exc)
                continue

            add_jsonld_from_object(
                getattr(collection_details, "json", {}),
                graph,
                stats,
                seen_jsonld_signatures,
                seen_resource_citation_tree_ids,
            )

            for child in getattr(collection_details, "children", []) or []:
                child_json = getattr(child, "json", {})
                child_id = safe_object_id(child_json) or str(getattr(child, "id", ""))
                child_type = str(child_json.get("@type", "")).lower()

                if "collection" in child_type:
                    if (
                        child_id not in visited_collection_ids
                        and child_id not in queued_collection_ids
                    ):
                        queue.append(child)
                        queued_collection_ids.add(child_id)
                        if pbar.total is not None:
                            pbar.total += 1
                            pbar.refresh()
                else:
                    if child_id in visited_resource_ids:
                        continue
                    should_continue = _process_resource(
                        dts_client=dts_client,
                        resource=child,
                        graph=graph,
                        stats=stats,
                        seen_resource_ids=visited_resource_ids,
                        seen_citation_tree_signatures=visited_citation_tree_signatures,
                        seen_citable_unit_ids=visited_citable_unit_ids,
                        seen_jsonld_signatures=seen_jsonld_signatures,
                        seen_resource_citation_tree_ids=seen_resource_citation_tree_ids,
                        max_resources=max_resources,
                        pbar=pbar,
                    )
                    if not should_continue:
                        return
    finally:
        pbar.close()


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    configure_logging(args.log_level, args.log_file)

    graph = Graph()
    stats = HarvestStats()

    LOGGER.info("Initializing DTS client for %s", args.entrypoint)
    dts_client = DTS_API(args.entrypoint, enable_validation=False)

    LOGGER.info("Starting DTS crawl")
    crawl(dts_client, graph, stats, args.max_resources, args.collection_id)

    graph.serialize(destination=args.output, format="turtle")
    LOGGER.info("Wrote merged Turtle file to %s", args.output)
    LOGGER.info("Collections visited: %s", stats.visited_collections)
    LOGGER.info("Resources visited: %s", stats.visited_resources)
    LOGGER.info("CitationTrees visited: %s", stats.visited_citation_trees)
    LOGGER.info("CitableUnits visited: %s", stats.visited_citable_units)
    LOGGER.info("Navigation requests sent: %s", stats.navigation_requests)
    LOGGER.info("JSON-LD objects seen: %s", stats.jsonld_objects_seen)
    LOGGER.info("JSON-LD objects deduplicated: %s", stats.jsonld_objects_deduplicated)
    LOGGER.info("JSON-LD objects parsed: %s", stats.jsonld_objects_parsed)
    LOGGER.info("JSON-LD objects skipped: %s", stats.jsonld_objects_skipped)
    LOGGER.info("Triples written: %s", len(graph))
    print(f"Done. Wrote Turtle to {args.output}. Logs: {args.log_file}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
