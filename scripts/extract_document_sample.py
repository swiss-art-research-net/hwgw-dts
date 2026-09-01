#!/usr/bin/env python3
"""Extract a single-document Turtle sample from a combined HWGW DTS graph.

Produces a self-contained subgraph for one CitableUnit (page, paragraph, …):
DTS Resource and Navigation context, CRM/CRMdig enrichment, and HWGW entity
stubs linked via ``crm:P67_refers_to``.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import DCTERMS, RDF, RDFS, XSD

CRM = Namespace("http://www.cidoc-crm.org/cidoc-crm/")
CRMDIG = Namespace("http://www.ics.forth.gr/isl/CRMdig/")
DTS = Namespace("https://dtsapi.org/v1.0#")
HWGW_DTS_BASE = "https://hwgw.uzh.ch/dts/"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract a single-document sample from a combined Turtle graph."
    )
    parser.add_argument(
        "--input",
        default="data/combined/s03_ed_combined.ttl",
        help="Combined Turtle graph.",
    )
    parser.add_argument(
        "--unit-id",
        default="s03-pg85",
        help="dts:identifier of the CitableUnit to extract.",
    )
    parser.add_argument(
        "--external-index",
        default="data/external/register-entity-index.csv",
        help="Entity register CSV (for CRM class stubs on entity URIs).",
    )
    parser.add_argument(
        "--output",
        default="data/samples/s03-pg85_document.ttl",
        help="Path to output Turtle sample.",
    )
    return parser


def bind_namespaces(graph: Graph) -> None:
    graph.bind("crm", CRM)
    graph.bind("crmdig", CRMDIG)
    graph.bind("dts", DTS)
    graph.bind("dct", DCTERMS)
    graph.bind("rdfs", RDFS)
    graph.bind("xsd", XSD)


def load_external_crm_classes(path: Path) -> dict[str, URIRef]:
    mapping: dict[str, URIRef] = {}
    with path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            uri = row.get("uri", "").strip()
            crm_class = row.get("crm", "").strip()
            if uri and crm_class:
                mapping[uri] = CRM[crm_class]
    return mapping


def find_citable_unit(graph: Graph, unit_id: str) -> URIRef | BNode | None:
    for subject in graph.subjects(DTS.identifier, Literal(unit_id)):
        if (subject, RDF.type, DTS.CitableUnit) in graph:
            return subject  # type: ignore[return-value]
    return None


def copy_node_closure(source: Graph, target: Graph, root: URIRef | BNode) -> None:
    visited: set[URIRef | BNode] = set()
    queue: list[URIRef | BNode] = [root]
    while queue:
        node = queue.pop()
        if node in visited:
            continue
        visited.add(node)
        for subject, predicate, obj in source.triples((node, None, None)):
            target.add((subject, predicate, obj))
            if isinstance(obj, BNode) and obj not in visited:
                queue.append(obj)


def copy_resource_context(
    source: Graph,
    target: Graph,
    citable_unit: URIRef | BNode,
) -> None:
    navigation = None
    for candidate in source.subjects(DTS.member, citable_unit):
        if (candidate, RDF.type, DTS.Navigation) in source:
            navigation = candidate
            break
    if navigation is None:
        raise ValueError(f"No dts:Navigation member found for CitableUnit {citable_unit!r}")

    resource = source.value(navigation, DTS.resource)
    if resource is None:
        raise ValueError(f"Navigation {navigation!r} has no dts:resource")

    copy_node_closure(source, target, resource)  # type: ignore[arg-type]

    target.add((navigation, RDF.type, DTS.Navigation))
    for predicate in (DTS.dtsVersion, DTS.resource):
        value = source.value(navigation, predicate)
        if value is not None:
            target.add((navigation, predicate, value))
    target.add((navigation, DTS.member, citable_unit))


def copy_citable_unit(source: Graph, target: Graph, citable_unit: URIRef | BNode) -> None:
    for triple in source.triples((citable_unit, None, None)):
        target.add(triple)


def copy_digital_object(source: Graph, target: Graph, unit_id: str) -> set[URIRef]:
    digital_object = URIRef(f"{HWGW_DTS_BASE}{unit_id}")
    if (digital_object, RDF.type, CRMDIG.D1_Digital_Object) not in source:
        raise ValueError(f"No crmdig:D1_Digital_Object found for unit {unit_id!r}")

    entity_uris: set[URIRef] = set()
    visited: set[URIRef | BNode] = set()
    queue: list[URIRef | BNode] = [digital_object]

    while queue:
        node = queue.pop()
        if node in visited:
            continue
        visited.add(node)
        for subject, predicate, obj in source.triples((node, None, None)):
            target.add((subject, predicate, obj))
            if predicate == CRM.P67_refers_to and isinstance(obj, URIRef):
                entity_uris.add(obj)
            if isinstance(obj, BNode) and obj not in visited:
                queue.append(obj)

    return entity_uris


def add_entity_stubs(
    target: Graph,
    entity_uris: set[URIRef],
    crm_classes: dict[str, URIRef],
) -> None:
    for entity_uri in sorted(entity_uris, key=str):
        crm_class = crm_classes.get(str(entity_uri))
        if crm_class is not None:
            target.add((entity_uri, RDF.type, crm_class))


def extract_sample(
    source: Graph,
    unit_id: str,
    crm_classes: dict[str, URIRef],
) -> Graph:
    citable_unit = find_citable_unit(source, unit_id)
    if citable_unit is None:
        raise ValueError(f"No dts:CitableUnit with dts:identifier {unit_id!r}")

    sample = Graph()
    bind_namespaces(sample)
    copy_resource_context(source, sample, citable_unit)
    copy_citable_unit(source, sample, citable_unit)
    entity_uris = copy_digital_object(source, sample, unit_id)
    add_entity_stubs(sample, entity_uris, crm_classes)
    return sample


def main() -> int:
    args = build_parser().parse_args()
    input_path = Path(args.input)
    if not input_path.is_file():
        raise FileNotFoundError(f"Combined graph not found: {input_path}")

    source = Graph()
    source.parse(input_path, format="turtle")

    external_index = Path(args.external_index)
    crm_classes = load_external_crm_classes(external_index) if external_index.is_file() else {}

    sample = extract_sample(source, args.unit_id.strip(), crm_classes)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = sample.serialize(format="turtle")
    if isinstance(serialized, bytes):
        serialized = serialized.decode("utf-8")
    header = (
        f"# HWGW DTS document sample: CitableUnit {args.unit_id!r}\n"
        "#\n"
        "# Layers:\n"
        "#   1. dts:Resource + dts:Navigation (citation layer)\n"
        "#   2. dts:CitableUnit → crmdig:D1_Digital_Object (CRM bridge)\n"
        "#   3. crm:P67_refers_to → HWGW entity URIs (passage mentions)\n"
        "#\n"
    )
    output_path.write_text(header + serialized, encoding="utf-8")

    print(
        f"Wrote {len(sample)} triples for CitableUnit {args.unit_id!r} "
        f"to {output_path} ({len(list(sample.subjects(RDF.type, CRMDIG.D1_Digital_Object)))} D1, "
        f"{len(list(sample.triples((None, CRM.P67_refers_to, None))))} entity links)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
