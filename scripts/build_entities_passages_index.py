#!/usr/bin/env python3
"""Build an entities–passages index from DTS TEI documents.

For a given Resource and CitationTree:
1. list CitableUnits via the navigation endpoint
2. fetch TEI for each unit via the document endpoint
3. extract <rs> mentions
4. write a CSV index with rs@type, rs@ref, citable unit id, and document URI
"""

from __future__ import annotations

import argparse
import csv
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Iterator

import requests
from dts_validator.client import DTS_API, DTS_Resource
from tqdm.auto import tqdm
from uritemplate import URITemplate

LOGGER = logging.getLogger("dts_entities_index")


@dataclass
class IndexStats:
    citation_trees_available: int = 0
    citable_units_seen: int = 0
    citable_units_processed: int = 0
    document_requests: int = 0
    document_failures: int = 0
    rs_mentions: int = 0


@dataclass
class IndexRow:
    rs_type: str
    rs_ref: str
    citable_unit_id: str
    document_uri: str
    citation_tree: str
    rs_text: str = ""
    citable_unit_cite_type: str = ""


@dataclass
class BuildResult:
    rows: list[IndexRow] = field(default_factory=list)
    stats: IndexStats = field(default_factory=IndexStats)


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
        description="Build an entities–passages index from DTS TEI <rs> mentions."
    )
    parser.add_argument(
        "--entrypoint",
        default="http://rs4.ethz.ch/dts/",
        help="DTS entry endpoint URL.",
    )
    parser.add_argument(
        "--resource-id",
        required=True,
        help="DTS Resource @id to index.",
    )
    parser.add_argument(
        "--tree",
        default=None,
        help=(
            "CitationTree identifier to use (e.g. logical_structure). "
            "Defaults to the first CitationTree declared on the resource."
        ),
    )
    parser.add_argument(
        "--cite-type",
        default=None,
        help=(
            "Optional CitableUnit citeType filter (e.g. paragraph). "
            "If omitted, all units returned by navigation are processed."
        ),
    )
    parser.add_argument(
        "--leaf-only",
        action="store_true",
        help="Only process CitableUnits at the deepest level of the selected tree.",
    )
    parser.add_argument(
        "--max-units",
        type=int,
        default=None,
        help="Optional safety cap for processed CitableUnits.",
    )
    parser.add_argument(
        "--output",
        default="data/index/entities_passages_index.csv",
        help="Path to output CSV index.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level.",
    )
    parser.add_argument(
        "--log-file",
        default="data/logs/entities_index.log",
        help="Path to log file (logging is written here, not stdout).",
    )
    return parser


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def extract_citation_trees(resource_json: dict[str, Any]) -> list[dict[str, Any]]:
    trees: list[dict[str, Any]] = []
    for tree in _as_list(resource_json.get("citationTrees")):
        if isinstance(tree, dict):
            trees.append(tree)
    return trees


def select_citation_tree(
    trees: list[dict[str, Any]],
    tree_id: str | None,
) -> dict[str, Any]:
    if not trees:
        raise ValueError("Resource declares no CitationTrees.")

    if tree_id is None:
        selected = trees[0]
        selected_id = str(selected.get("identifier") or selected.get("citeType") or "")
        LOGGER.info(
            "No --tree provided; using first CitationTree: %s",
            selected_id or "<unnamed>",
        )
        return selected

    for tree in trees:
        identifier = str(tree.get("identifier") or "").strip()
        if identifier == tree_id:
            return tree

    available = [
        str(tree.get("identifier") or tree.get("citeType") or "<unnamed>") for tree in trees
    ]
    raise ValueError(
        f"CitationTree '{tree_id}' not found on resource. Available: {', '.join(available)}"
    )


def tree_parameter(tree: dict[str, Any]) -> str | None:
    identifier = tree.get("identifier")
    if isinstance(identifier, str) and identifier.strip():
        return identifier.strip()
    return None


def load_resource(dts_client: DTS_API, resource_id: str) -> DTS_Resource:
    collection = dts_client.collections(id=resource_id)
    resource_json = getattr(collection, "json", {})
    if resource_json.get("@type") != "Resource":
        raise ValueError(
            f"Object '{resource_id}' is not a DTS Resource (got @type={resource_json.get('@type')!r})."
        )
    if "navigation" not in resource_json:
        raise ValueError(f"Resource '{resource_id}' is missing a navigation URI template.")
    if "document" not in resource_json:
        raise ValueError(f"Resource '{resource_id}' is missing a document URI template.")
    return DTS_Resource(resource_json)


def expand_endpoint(template: str, **params: Any) -> str:
    filtered = {key: value for key, value in params.items() if value is not None}
    return URITemplate(template).expand(filtered)


def fetch_citable_units(
    resource: DTS_Resource,
    tree: str | None,
) -> tuple[list[dict[str, Any]], str]:
    navigation_uri = expand_endpoint(
        resource.json["navigation"],
        resource=resource.id,
        down=-1,
        tree=tree,
    )
    LOGGER.info("Fetching CitableUnits: %s", navigation_uri)
    response = requests.get(navigation_uri, timeout=60)
    response.raise_for_status()
    payload = response.json()
    members = [member for member in _as_list(payload.get("member")) if isinstance(member, dict)]
    return members, navigation_uri


def filter_citable_units(
    units: list[dict[str, Any]],
    cite_type: str | None,
    leaf_only: bool,
) -> list[dict[str, Any]]:
    filtered = units
    if cite_type:
        filtered = [
            unit
            for unit in filtered
            if str(unit.get("citeType", "")).strip() == cite_type
        ]
    if leaf_only and filtered:
        levels = [
            int(unit["level"])
            for unit in filtered
            if str(unit.get("level", "")).isdigit() or isinstance(unit.get("level"), int)
        ]
        if levels:
            max_level = max(levels)
            filtered = [
                unit
                for unit in filtered
                if int(unit.get("level", -1)) == max_level
            ]
    return filtered


def document_uri_for_unit(
    resource: DTS_Resource,
    unit_id: str,
    tree: str | None,
) -> str:
    return expand_endpoint(
        resource.json["document"],
        resource=resource.id,
        ref=unit_id,
        tree=tree,
    )


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def iter_rs_elements(tei_xml: str) -> Iterator[ET.Element]:
    root = ET.fromstring(tei_xml)
    for element in root.iter():
        if local_name(element.tag) == "rs":
            yield element


def extract_rs_rows(
    tei_xml: str,
    unit: dict[str, Any],
    document_uri: str,
    citation_tree: str,
) -> list[IndexRow]:
    unit_id = str(unit.get("identifier") or "")
    cite_type = str(unit.get("citeType") or "")
    rows: list[IndexRow] = []
    for rs_el in iter_rs_elements(tei_xml):
        rows.append(
            IndexRow(
                rs_type=str(rs_el.attrib.get("type") or ""),
                rs_ref=str(rs_el.attrib.get("ref") or ""),
                citable_unit_id=unit_id,
                document_uri=document_uri,
                citation_tree=citation_tree,
                rs_text="".join(rs_el.itertext()).strip(),
                citable_unit_cite_type=cite_type,
            )
        )
    return rows


def build_index(
    resource: DTS_Resource,
    tree: dict[str, Any],
    cite_type: str | None,
    leaf_only: bool,
    max_units: int | None,
) -> BuildResult:
    result = BuildResult()
    tree_param = tree_parameter(tree)
    units, _ = fetch_citable_units(resource, tree_param)
    result.stats.citable_units_seen = len(units)
    selected_units = filter_citable_units(units, cite_type=cite_type, leaf_only=leaf_only)
    LOGGER.info(
        "Selected %s/%s CitableUnits (cite_type=%s, leaf_only=%s)",
        len(selected_units),
        len(units),
        cite_type or "*",
        leaf_only,
    )

    pbar = tqdm(selected_units, desc="Indexing passages", unit="unit")
    try:
        for unit in pbar:
            if max_units is not None and result.stats.citable_units_processed >= max_units:
                LOGGER.warning("Reached max units cap (%s); stopping.", max_units)
                break

            unit_id = str(unit.get("identifier") or "").strip()
            if not unit_id:
                LOGGER.debug("Skipping CitableUnit without identifier: %s", unit)
                continue

            doc_uri = document_uri_for_unit(resource, unit_id, tree_param)
            LOGGER.debug("Fetching document: %s", doc_uri)
            result.stats.document_requests += 1
            response = requests.get(doc_uri, timeout=60)
            if response.status_code != 200:
                result.stats.document_failures += 1
                LOGGER.warning(
                    "Document request failed for unit %s: HTTP %s",
                    unit_id,
                    response.status_code,
                )
                continue

            try:
                rows = extract_rs_rows(response.text, unit, doc_uri, tree_param or "")
            except ET.ParseError as exc:
                result.stats.document_failures += 1
                LOGGER.warning("Failed parsing TEI for unit %s: %s", unit_id, exc)
                continue

            result.rows.extend(rows)
            result.stats.rs_mentions += len(rows)
            result.stats.citable_units_processed += 1
            pbar.set_postfix(rs=result.stats.rs_mentions, failures=result.stats.document_failures)
    finally:
        pbar.close()

    return result


def write_csv(path: str, rows: list[IndexRow]) -> None:
    fieldnames = [
        "rs_type",
        "rs_ref",
        "rs_text",
        "citation_tree",
        "citable_unit_cite_type",
        "citable_unit_id",
        "document_uri",
    ]
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "rs_type": row.rs_type,
                    "rs_ref": row.rs_ref,
                    "rs_text": row.rs_text,
                    "citation_tree": row.citation_tree,
                    "citable_unit_cite_type": row.citable_unit_cite_type,
                    "citable_unit_id": row.citable_unit_id,
                    "document_uri": row.document_uri,
                }
            )


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    configure_logging(args.log_level, args.log_file)

    LOGGER.info("Initializing DTS client for %s", args.entrypoint)
    dts_client = DTS_API(args.entrypoint, enable_validation=False)

    LOGGER.info("Loading resource %s", args.resource_id)
    resource = load_resource(dts_client, args.resource_id)
    trees = extract_citation_trees(resource.json)
    stats_preview = IndexStats(citation_trees_available=len(trees))
    selected_tree = select_citation_tree(trees, args.tree)
    tree_id = tree_parameter(selected_tree) or str(selected_tree.get("citeType") or "")
    LOGGER.info("Using CitationTree: %s", tree_id)

    result = build_index(
        resource=resource,
        tree=selected_tree,
        cite_type=args.cite_type,
        leaf_only=args.leaf_only,
        max_units=args.max_units,
    )
    result.stats.citation_trees_available = stats_preview.citation_trees_available

    write_csv(args.output, result.rows)
    LOGGER.info("Wrote entities–passages index to %s", args.output)
    LOGGER.info("CitationTrees available: %s", result.stats.citation_trees_available)
    LOGGER.info("CitableUnits seen: %s", result.stats.citable_units_seen)
    LOGGER.info("CitableUnits processed: %s", result.stats.citable_units_processed)
    LOGGER.info("Document requests: %s", result.stats.document_requests)
    LOGGER.info("Document failures: %s", result.stats.document_failures)
    LOGGER.info("rs mentions indexed: %s", result.stats.rs_mentions)
    print(
        f"Done. Wrote {result.stats.rs_mentions} rows to {args.output}. "
        f"Logs: {args.log_file}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
