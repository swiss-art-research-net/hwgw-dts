#!/usr/bin/env python3
"""Combine harvested DTS Turtle with entity–passage and external entity indices.

Inputs:
1. A Turtle file produced by ``harvest_jsonld_to_ttl.py``.
2. An entities–passages CSV produced by ``build_entities_passages_index.py``.
3. A local-ID-to-mapped-URI entity index CSV (``register-entity-index.csv``).

Entities–passages CSV columns::

    rs_type, rs_ref, rs_text, citation_tree,
    citable_unit_cite_type, citable_unit_id, document_uri

External entity index CSV columns (see hwgw-dts issue #4)::

    type, crm, local_id, uri

Rows are joined on ``entities_passages.rs_ref = external_index.local_id``.
Entity URIs follow ``https://hwgw.uzh.ch/{type}/{local_id}``.

The script loads all three sources, joins passage mentions to external entity
records, and writes an enriched RDF graph.
"""

from __future__ import annotations

import argparse
import csv
import logging
from dataclasses import dataclass, field
from pathlib import Path

from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import DCTERMS, RDF, RDFS, XSD
from rdflib.term import Node
from tqdm.auto import tqdm

LOGGER = logging.getLogger("dts_entity_graph")

CRM = Namespace("http://www.cidoc-crm.org/cidoc-crm/")
CRMDIG = Namespace("http://www.ics.forth.gr/isl/CRMdig/")
DTS = Namespace("https://dtsapi.org/v1.0#")
HWGW_DTS_BASE = "https://hwgw.uzh.ch/dts/"
DTS_DOCUMENT_URI_LABEL = "DTS Document URI"

ENTITIES_PASSAGES_COLUMNS = (
    "rs_type",
    "rs_ref",
    "rs_text",
    "citation_tree",
    "citable_unit_cite_type",
    "citable_unit_id",
    "document_uri",
)

EXTERNAL_INDEX_COLUMNS = (
    "type",
    "crm",
    "local_id",
    "uri",
)

PASSAGE_JOIN_COLUMN = "rs_ref"
EXTERNAL_JOIN_COLUMN = "local_id"


@dataclass
class PassageEntityRow:
    rs_type: str
    rs_ref: str
    rs_text: str
    citation_tree: str
    citable_unit_cite_type: str
    citable_unit_id: str
    document_uri: str


@dataclass
class ExternalEntityRow:
    entity_type: str
    crm: str
    local_id: str
    uri: str


@dataclass
class CombineStats:
    ttl_triples_loaded: int = 0
    passage_rows_loaded: int = 0
    external_rows_loaded: int = 0
    external_rows_indexed: int = 0
    passage_rows_with_external_match: int = 0
    passage_rows_without_external_match: int = 0
    citable_units_linked: int = 0
    citable_units_missing_from_harvest: int = 0
    entity_references_added: int = 0
    triples_added: int = 0
    unmatched_rs_refs: set[str] = field(default_factory=set)


@dataclass
class CombineResult:
    graph: Graph
    stats: CombineStats = field(default_factory=CombineStats)


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
        description=(
            "Combine harvested DTS Turtle with an entities–passages index "
            "and an external entity index."
        )
    )
    parser.add_argument(
        "--ttl",
        required=True,
        help="Path to harvested Turtle file (from harvest_jsonld_to_ttl.py).",
    )
    parser.add_argument(
        "--entities-passages",
        required=True,
        help=(
            "Path to entities–passages CSV "
            "(from build_entities_passages_index.py)."
        ),
    )
    parser.add_argument(
        "--external-index",
        required=True,
        help=(
            "Path to local-ID-to-mapped-URI entity index CSV "
            "(columns: type, crm, local_id, uri)."
        ),
    )
    parser.add_argument(
        "--output",
        default="data/combined/combined.ttl",
        help="Path to output Turtle file.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level.",
    )
    parser.add_argument(
        "--log-file",
        default="data/logs/combine_entity_graph.log",
        help="Path to log file (logging is written here, not stdout).",
    )
    return parser


def _read_csv_rows(path: str | Path) -> tuple[list[str], list[dict[str, str]]]:
    csv_path = Path(path)
    if not csv_path.is_file():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV file has no header row: {csv_path}")
        fieldnames = list(reader.fieldnames)
        rows = [{key: str(value or "") for key, value in row.items()} for row in reader]
    return fieldnames, rows


def load_ttl(path: str | Path) -> Graph:
    ttl_path = Path(path)
    if not ttl_path.is_file():
        raise FileNotFoundError(f"Turtle file not found: {ttl_path}")

    graph = Graph()
    LOGGER.info("Loading Turtle graph from %s", ttl_path)
    graph.parse(ttl_path, format="turtle")
    LOGGER.info("Loaded %s triples from %s", len(graph), ttl_path)
    return graph


def load_entities_passages_csv(path: str | Path) -> list[PassageEntityRow]:
    fieldnames, rows = _read_csv_rows(path)
    missing = [column for column in ENTITIES_PASSAGES_COLUMNS if column not in fieldnames]
    if missing:
        raise ValueError(
            f"Entities–passages CSV is missing required columns: {', '.join(missing)}"
        )

    parsed: list[PassageEntityRow] = []
    for row in rows:
        parsed.append(
            PassageEntityRow(
                rs_type=row["rs_type"],
                rs_ref=row["rs_ref"],
                rs_text=row["rs_text"],
                citation_tree=row["citation_tree"],
                citable_unit_cite_type=row["citable_unit_cite_type"],
                citable_unit_id=row["citable_unit_id"],
                document_uri=row["document_uri"],
            )
        )

    LOGGER.info("Loaded %s entities–passages rows from %s", len(parsed), path)
    return parsed


def load_external_index_csv(path: str | Path) -> dict[str, ExternalEntityRow]:
    fieldnames, rows = _read_csv_rows(path)
    missing = [column for column in EXTERNAL_INDEX_COLUMNS if column not in fieldnames]
    if missing:
        raise ValueError(
            f"External index CSV is missing required columns: {', '.join(missing)}"
        )

    indexed: dict[str, ExternalEntityRow] = {}
    duplicate_local_ids: set[str] = set()
    for row in rows:
        local_id = row["local_id"].strip()
        if not local_id:
            LOGGER.debug("Skipping external index row with empty local_id")
            continue
        if local_id in indexed:
            duplicate_local_ids.add(local_id)
        indexed[local_id] = ExternalEntityRow(
            entity_type=row["type"],
            crm=row["crm"],
            local_id=local_id,
            uri=row["uri"],
        )

    if duplicate_local_ids:
        LOGGER.warning(
            "External index contains %s duplicate local_id values; keeping last row for each.",
            len(duplicate_local_ids),
        )

    LOGGER.info(
        "Loaded %s external index rows (%s indexed by local_id) from %s",
        len(rows),
        len(indexed),
        path,
    )
    return indexed


def bind_namespaces(graph: Graph) -> None:
    graph.bind("crm", CRM)
    graph.bind("crmdig", CRMDIG)
    graph.bind("dts", DTS)
    graph.bind("dct", DCTERMS)
    graph.bind("rdfs", RDFS)
    graph.bind("xsd", XSD)


def digital_object_uri(citable_unit_id: str) -> URIRef:
    return URIRef(f"{HWGW_DTS_BASE}{citable_unit_id}")


def build_citable_unit_index(graph: Graph) -> dict[str, Node]:
    """Map ``dts:identifier`` values to existing ``dts:CitableUnit`` nodes."""
    index: dict[str, Node] = {}
    duplicate_identifiers: set[str] = set()
    for subject in graph.subjects(RDF.type, DTS.CitableUnit):
        identifier = graph.value(subject, DTS.identifier)
        if identifier is None:
            continue
        key = str(identifier)
        if key in index:
            duplicate_identifiers.add(key)
        index[key] = subject

    if duplicate_identifiers:
        LOGGER.warning(
            "Harvested graph contains %s duplicate dts:identifier values on CitableUnits; "
            "using the last seen node for each.",
            len(duplicate_identifiers),
        )

    LOGGER.info("Indexed %s CitableUnits from harvested graph", len(index))
    return index


def _ensure_citable_unit_structure(
    graph: Graph,
    passage_row: PassageEntityRow,
    citable_unit_index: dict[str, Node],
    initialized_units: set[str],
    missing_units: set[str],
    stats: CombineStats,
) -> bool:
    """Attach D1 representation metadata to an existing harvested CitableUnit.

    Returns ``True`` when the unit was linked, ``False`` when skipped.
    """
    unit_id = passage_row.citable_unit_id.strip()
    if not unit_id:
        LOGGER.debug("Skipping passage row with empty citable_unit_id")
        return False
    if unit_id in initialized_units:
        return True

    cu_node = citable_unit_index.get(unit_id)
    if cu_node is None:
        if unit_id not in missing_units:
            missing_units.add(unit_id)
            stats.citable_units_missing_from_harvest += 1
            LOGGER.warning(
                "No harvested dts:CitableUnit with dts:identifier %r; "
                "skipping CRM bridge for this unit.",
                unit_id,
            )
        return False

    do_uri = digital_object_uri(unit_id)
    graph.add((cu_node, CRM.P67_refers_to, do_uri))

    graph.add((do_uri, RDF.type, CRMDIG.D1_Digital_Object))

    identifier = BNode()
    identifier_type = BNode()
    graph.add((identifier, RDF.type, CRM.E42_Identifier))
    graph.add((do_uri, CRM.P1_is_identified_by, identifier))
    graph.add((identifier, CRM.P2_has_type, identifier_type))
    graph.add((identifier_type, RDF.type, CRM.E55_Type))
    graph.add((identifier_type, RDFS.label, Literal(DTS_DOCUMENT_URI_LABEL, lang="en")))
    graph.add(
        (
            identifier,
            RDF.value,
            Literal(passage_row.document_uri, datatype=XSD.anyURI),
        )
    )

    initialized_units.add(unit_id)
    stats.citable_units_linked += 1
    return True


def _add_entity_reference(
    graph: Graph,
    passage_row: PassageEntityRow,
    external_row: ExternalEntityRow,
    entity_links: set[tuple[str, str]],
    stats: CombineStats,
) -> None:
    unit_id = passage_row.citable_unit_id.strip()
    if not unit_id:
        return

    link_key = (unit_id, external_row.uri)
    if link_key in entity_links:
        return

    do_uri = digital_object_uri(unit_id)
    graph.add((do_uri, CRM.P67_refers_to, URIRef(external_row.uri)))
    entity_links.add(link_key)
    stats.entity_references_added += 1


def combine_graph(
    graph: Graph,
    passage_rows: list[PassageEntityRow],
    external_index: dict[str, ExternalEntityRow],
) -> CombineResult:
    """Join passage mentions with external entities and enrich the RDF graph."""
    result = CombineResult(graph=graph)
    result.stats.ttl_triples_loaded = len(graph)
    result.stats.passage_rows_loaded = len(passage_rows)
    result.stats.external_rows_loaded = sum(1 for _ in external_index.values())
    result.stats.external_rows_indexed = len(external_index)

    citable_unit_index = build_citable_unit_index(graph)
    initial_triple_count = len(graph)
    initialized_units: set[str] = set()
    missing_units: set[str] = set()
    entity_links: set[tuple[str, str]] = set()

    pbar = tqdm(passage_rows, desc="Linking entities to passages", unit="mention")
    try:
        for passage_row in pbar:
            external_row = external_index.get(passage_row.rs_ref.strip())
            if external_row is None:
                result.stats.passage_rows_without_external_match += 1
                if passage_row.rs_ref.strip():
                    result.stats.unmatched_rs_refs.add(passage_row.rs_ref.strip())
                continue

            result.stats.passage_rows_with_external_match += 1
            _add_passage_entity_triples(
                graph=result.graph,
                passage_row=passage_row,
                external_row=external_row,
                citable_unit_index=citable_unit_index,
                initialized_units=initialized_units,
                missing_units=missing_units,
                entity_links=entity_links,
                stats=result.stats,
            )
            pbar.set_postfix(
                linked=result.stats.citable_units_linked,
                refs=result.stats.entity_references_added,
            )
    finally:
        pbar.close()

    result.stats.triples_added = len(graph) - initial_triple_count
    return result


def _add_passage_entity_triples(
    graph: Graph,
    passage_row: PassageEntityRow,
    external_row: ExternalEntityRow,
    citable_unit_index: dict[str, Node],
    initialized_units: set[str],
    missing_units: set[str],
    entity_links: set[tuple[str, str]],
    stats: CombineStats,
) -> None:
    """Emit RDF for one matched passage mention.

    Reuses existing blank-node ``dts:CitableUnit`` nodes from the harvested
    graph (matched by ``dts:identifier``). Units absent from the harvest are
    skipped. Each linked unit gets ``crm:P67_refers_to`` to a
    ``crmdig:D1_Digital_Object`` carrying the DTS document URI as an
    ``crm:E42_Identifier``. Each distinct entity referenced on that unit adds
    ``crm:P67_refers_to`` from the D1 to the mapped HWGW entity URI.
    """
    if not _ensure_citable_unit_structure(
        graph=graph,
        passage_row=passage_row,
        citable_unit_index=citable_unit_index,
        initialized_units=initialized_units,
        missing_units=missing_units,
        stats=stats,
    ):
        return

    _add_entity_reference(
        graph=graph,
        passage_row=passage_row,
        external_row=external_row,
        entity_links=entity_links,
        stats=stats,
    )


def write_ttl(graph: Graph, path: str | Path) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    bind_namespaces(graph)
    serialized = graph.serialize(format="turtle")
    if isinstance(serialized, bytes):
        serialized = serialized.decode("utf-8")
    output_path.write_text(serialized, encoding="utf-8")
    LOGGER.info("Wrote combined Turtle graph to %s", output_path)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    configure_logging(args.log_level, args.log_file)

    graph = load_ttl(args.ttl)
    passage_rows = load_entities_passages_csv(args.entities_passages)
    external_index = load_external_index_csv(args.external_index)
    LOGGER.info(
        "Joining %s on %s = external_index.%s",
        args.entities_passages,
        PASSAGE_JOIN_COLUMN,
        EXTERNAL_JOIN_COLUMN,
    )

    result = combine_graph(graph, passage_rows, external_index)
    write_ttl(result.graph, args.output)

    stats = result.stats
    LOGGER.info("TTL triples loaded: %s", stats.ttl_triples_loaded)
    LOGGER.info("Passage rows loaded: %s", stats.passage_rows_loaded)
    LOGGER.info("External rows indexed: %s", stats.external_rows_indexed)
    LOGGER.info("Passage rows with external match: %s", stats.passage_rows_with_external_match)
    LOGGER.info(
        "Passage rows without external match: %s",
        stats.passage_rows_without_external_match,
    )
    LOGGER.info("Triples added: %s", stats.triples_added)
    LOGGER.info("Citable units linked: %s", stats.citable_units_linked)
    LOGGER.info(
        "Citable units missing from harvest: %s",
        stats.citable_units_missing_from_harvest,
    )
    LOGGER.info("Entity references added: %s", stats.entity_references_added)
    if stats.unmatched_rs_refs:
        sample = sorted(stats.unmatched_rs_refs)[:10]
        LOGGER.info(
            "Unmatched rs_ref sample (%s total): %s",
            len(stats.unmatched_rs_refs),
            ", ".join(sample),
        )

    print(
        f"Done. Wrote {len(result.graph)} triples to {args.output}. "
        f"Matched {stats.passage_rows_with_external_match}/{stats.passage_rows_loaded} "
        f"passage mentions; linked {stats.citable_units_linked} citable units and "
        f"{stats.entity_references_added} entity references. "
        f"Missing from harvest: {stats.citable_units_missing_from_harvest}. "
        f"Logs: {args.log_file}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
