from __future__ import annotations

import sys
from pathlib import Path

from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from verify_pipeline import (  # noqa: E402
    CRM,
    CRMDIG,
    DTS,
    run_verification,
    verify_entity_passage_pairs,
    verify_entity_register,
)

HWGW_DTS_BASE = "https://hwgw.uzh.ch/dts/"


def test_entity_register_check_passes() -> None:
    rows = [{"rs_ref": "gnd-1", "citable_unit_id": "pg1"}]
    external = {"gnd-1": "https://hwgw.uzh.ch/person/gnd-1"}
    result = verify_entity_register(rows, external)  # type: ignore[arg-type]
    assert result.ok


def test_entity_register_check_fails_on_missing() -> None:
    rows = [{"rs_ref": "missing-ref", "citable_unit_id": "pg1"}]
    result = verify_entity_register(rows, {})  # type: ignore[arg-type]
    assert not result.ok
    assert result.missing == ["missing-ref"]


def test_entity_passage_pairs_in_combined_graph() -> None:
    combined = Graph()
    unit_id = "s03-pg85"
    entity_uri = URIRef("https://hwgw.uzh.ch/person/gnd-118538969")
    do_uri = URIRef(f"{HWGW_DTS_BASE}{unit_id}")
    combined.add((do_uri, RDF.type, CRMDIG.D1_Digital_Object))
    combined.add((do_uri, CRM.P67_refers_to, entity_uri))

    rows = [
        {
            "rs_ref": "gnd-118538969",
            "citable_unit_id": unit_id,
        }
    ]
    external = {"gnd-118538969": str(entity_uri)}
    result = verify_entity_passage_pairs(rows, external, combined)  # type: ignore[arg-type]
    assert result.ok


def test_entity_passage_pairs_missing_link() -> None:
    combined = Graph()
    rows = [{"rs_ref": "gnd-1", "citable_unit_id": "pg1"}]
    external = {"gnd-1": "https://hwgw.uzh.ch/person/gnd-1"}
    result = verify_entity_passage_pairs(rows, external, combined)  # type: ignore[arg-type]
    assert not result.ok
    assert result.missing_pairs == [("pg1", "https://hwgw.uzh.ch/person/gnd-1")]


def test_run_verification_end_to_end(tmp_path: Path) -> None:
    harvest = Graph()
    cu = BNode()
    harvest.add((cu, RDF.type, DTS.CitableUnit))
    harvest.add((cu, DTS.identifier, Literal("pg1")))
    harvest_ttl = tmp_path / "harvest.ttl"
    harvest.serialize(destination=harvest_ttl, format="turtle")

    harvest_log = tmp_path / "harvest.log"
    harvest_log.write_text(
        "INFO Collections visited: 0\n"
        "INFO Resources visited: 0\n"
        "INFO CitableUnits visited: 1\n",
        encoding="utf-8",
    )

    passages_csv = tmp_path / "passages.csv"
    passages_csv.write_text(
        "rs_type,rs_ref,rs_text,citation_tree,citable_unit_cite_type,citable_unit_id,document_uri\n"
        "person,gnd-1,Alice,published_page,page,pg1,http://example/doc\n",
        encoding="utf-8",
    )

    external_csv = tmp_path / "external.csv"
    external_csv.write_text(
        "type,crm,local_id,uri\n"
        "person,E21_Person,gnd-1,https://hwgw.uzh.ch/person/gnd-1\n",
        encoding="utf-8",
    )

    combined = Graph()
    do_uri = URIRef(f"{HWGW_DTS_BASE}pg1")
    entity_uri = URIRef("https://hwgw.uzh.ch/person/gnd-1")
    combined.add((do_uri, RDF.type, CRMDIG.D1_Digital_Object))
    combined.add((do_uri, CRM.P67_refers_to, entity_uri))
    combined_ttl = tmp_path / "combined.ttl"
    combined.serialize(destination=combined_ttl, format="turtle")

    report = run_verification(
        harvest_ttl=harvest_ttl,
        harvest_log=harvest_log,
        entities_passages=passages_csv,
        external_index_path=external_csv,
        combined_ttl=combined_ttl,
    )
    assert report.ok
