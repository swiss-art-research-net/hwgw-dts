from __future__ import annotations

import sys
from pathlib import Path

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS, XSD

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from combine_entity_graph import (  # noqa: E402
    CRM,
    CRMDIG,
    DTS,
    ExternalEntityRow,
    PassageEntityRow,
    combine_graph,
    digital_object_uri,
)


def _sample_passage_row() -> PassageEntityRow:
    return PassageEntityRow(
        rs_type="person",
        rs_ref="gnd-118538969",
        rs_text="Salomon Geßner",
        citation_tree="published_page",
        citable_unit_cite_type="page",
        citable_unit_id="s03-pg85",
        document_uri=(
            "http://rs4.ethz.ch/dts/document/?resource=https%3A%2F%2Fexample.org%2Fdts%2Fcollections%2Fhwgw%2Fs03%2Fed"
            "&ref=s03-pg85&tree=published_page"
        ),
    )


def _sample_external_row() -> ExternalEntityRow:
    return ExternalEntityRow(
        entity_type="person",
        crm="E21_Person",
        local_id="gnd-118538969",
        uri="https://hwgw.uzh.ch/person/gnd-118538969",
    )


def _add_harvested_citable_unit(graph: Graph, unit_id: str) -> BNode:
    unit = BNode()
    graph.add((unit, RDF.type, DTS.CitableUnit))
    graph.add((unit, DTS.citeType, Literal("page")))
    graph.add((unit, DTS.identifier, Literal(unit_id)))
    return unit


def test_links_existing_blank_node_citable_unit_to_digital_object() -> None:
    graph = Graph()
    existing_unit = _add_harvested_citable_unit(graph, "s03-pg85")
    result = combine_graph(
        graph=graph,
        passage_rows=[_sample_passage_row()],
        external_index={"gnd-118538969": _sample_external_row()},
    )

    do_uri = digital_object_uri("s03-pg85")
    entity_uri = URIRef("https://hwgw.uzh.ch/person/gnd-118538969")

    citable_units = list(graph.subjects(RDF.type, DTS.CitableUnit))
    assert len(citable_units) == 1
    assert citable_units[0] == existing_unit
    assert (existing_unit, CRM.P138i_has_representation, do_uri) in graph

    assert (do_uri, RDF.type, CRMDIG.D1_Digital_Object) in graph
    assert (do_uri, CRM.P67_refers_to, entity_uri) in graph

    type_labels = {
        label
        for _, _, label in graph.triples((None, RDFS.label, None))
        if isinstance(label, Literal)
    }
    assert Literal("DTS Document URI", lang="en") in type_labels

    assert result.stats.citable_units_linked == 1
    assert result.stats.citable_units_missing_from_harvest == 0
    assert result.stats.entity_references_added == 1
    assert result.stats.triples_added > 0


def test_skips_units_missing_from_harvest() -> None:
    graph = Graph()
    result = combine_graph(
        graph=graph,
        passage_rows=[_sample_passage_row()],
        external_index={"gnd-118538969": _sample_external_row()},
    )

    assert len(list(graph.subjects(RDF.type, DTS.CitableUnit))) == 0
    assert len(list(graph.subjects(RDF.type, CRMDIG.D1_Digital_Object))) == 0
    assert result.stats.citable_units_linked == 0
    assert result.stats.citable_units_missing_from_harvest == 1
    assert result.stats.entity_references_added == 0


def test_duplicate_mentions_deduplicate_entity_links() -> None:
    graph = Graph()
    _add_harvested_citable_unit(graph, "s03-pg85")
    duplicate_row = PassageEntityRow(
        rs_type="person",
        rs_ref="gnd-118538969",
        rs_text="Geßner",
        citation_tree="published_page",
        citable_unit_cite_type="page",
        citable_unit_id="s03-pg85",
        document_uri=_sample_passage_row().document_uri,
    )
    result = combine_graph(
        graph=graph,
        passage_rows=[_sample_passage_row(), duplicate_row],
        external_index={"gnd-118538969": _sample_external_row()},
    )

    do_uri = digital_object_uri("s03-pg85")
    entity_uri = URIRef("https://hwgw.uzh.ch/person/gnd-118538969")
    refers_to = list(graph.triples((do_uri, CRM.P67_refers_to, entity_uri)))

    assert result.stats.citable_units_linked == 1
    assert result.stats.entity_references_added == 1
    assert len(refers_to) == 1
