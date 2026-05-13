#!/usr/bin/env python3
"""Harvest JSON-LD from a DTS API and merge into one Turtle file."""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from typing import Any

from dts_validator.client import DTS_API
from rdflib import Graph
from tqdm.auto import tqdm

LOGGER = logging.getLogger("dts_harvest")
DTS_CONTEXT_URL = "https://dtsapi.org/context/v1.0.json"


@dataclass
class HarvestStats:
    visited_collections: int = 0
    visited_resources: int = 0
    jsonld_objects_seen: int = 0
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
        default="merged.ttl",
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
        default="harvest.log",
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
) -> None:
    if not isinstance(obj, dict) or not has_jsonld_markers(obj):
        return

    candidate = normalize_prefixed_object(
        obj,
        key="dublinCore",
        namespace_uri="http://purl.org/dc/terms/",
    )
    candidate = ensure_context(candidate, default_context_url=DTS_CONTEXT_URL)
    stats.jsonld_objects_seen += 1
    if try_parse_jsonld(graph, candidate):
        stats.jsonld_objects_parsed += 1
    else:
        stats.jsonld_objects_skipped += 1


def safe_object_id(obj: Any) -> str | None:
    if isinstance(obj, dict):
        obj_id = obj.get("@id") or obj.get("id")
        return str(obj_id) if obj_id else None
    return None


def crawl(
    dts_client: DTS_API,
    graph: Graph,
    stats: HarvestStats,
    max_resources: int | None,
    collection_id: str | None,
) -> None:
    visited_collection_ids: set[str] = set()
    visited_resource_ids: set[str] = set()

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
            add_jsonld_from_object(getattr(collection, "json", {}), graph, stats)
            pbar.update(1)
            pbar.set_postfix(
                collections=stats.visited_collections,
                resources=stats.visited_resources,
            )

            try:
                collection_details = dts_client.collections(id=collection.id)
            except Exception as exc:  # pylint: disable=broad-except
                LOGGER.warning("Failed loading collection %s: %s", collection.id, exc)
                continue

            add_jsonld_from_object(getattr(collection_details, "json", {}), graph, stats)

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
                    if max_resources is not None and stats.visited_resources >= max_resources:
                        LOGGER.warning(
                            "Reached max resources cap (%s); stopping crawl.", max_resources
                        )
                        return
                    visited_resource_ids.add(child_id)
                    if pbar.total is not None:
                        pbar.total += 1
                    stats.visited_resources += 1
                    add_jsonld_from_object(child_json, graph, stats)
                    pbar.update(1)
                    pbar.set_postfix(
                        collections=stats.visited_collections,
                        resources=stats.visited_resources,
                    )

                    try:
                        resource_collection = dts_client.collections(id=child.id)
                        add_jsonld_from_object(
                            getattr(resource_collection, "json", {}), graph, stats
                        )
                    except Exception as exc:  # pylint: disable=broad-except
                        LOGGER.debug(
                            "Resource collection metadata unavailable for %s: %s",
                            child.id,
                            exc,
                        )
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
    LOGGER.info("JSON-LD objects seen: %s", stats.jsonld_objects_seen)
    LOGGER.info("JSON-LD objects parsed: %s", stats.jsonld_objects_parsed)
    LOGGER.info("JSON-LD objects skipped: %s", stats.jsonld_objects_skipped)
    LOGGER.info("Triples written: %s", len(graph))
    print(f"Done. Wrote Turtle to {args.output}. Logs: {args.log_file}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
