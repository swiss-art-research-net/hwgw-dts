#!/usr/bin/env python3
"""Verify data integrity across the HWGW DTS pipeline.

Checks:
1. Harvested DTS counts (from log) match Collection/Resource/CitableUnit
   counts in the harvest TTL export.
2. Every ``rs_ref`` in the entities–passages index appears in the external
   entity register.
3. Every entity–passage pair (with a register entry) is represented in the
   combined TTL as ``crm:P67_refers_to`` from the unit's D1 to the mapped URI.
4. Every ``citable_unit_id`` referenced in the index exists as a
   ``dts:CitableUnit`` in the harvest TTL.
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF

LOGGER = logging.getLogger("dts_pipeline_verify")

CRM = Namespace("http://www.cidoc-crm.org/cidoc-crm/")
CRMDIG = Namespace("http://www.ics.forth.gr/isl/CRMdig/")
DTS = Namespace("https://dtsapi.org/v1.0#")
HWGW_DTS_BASE = "https://hwgw.uzh.ch/dts/"

ENTITIES_PASSAGES_COLUMNS = (
    "rs_type",
    "rs_ref",
    "rs_text",
    "citation_tree",
    "citable_unit_cite_type",
    "citable_unit_id",
    "document_uri",
)

EXTERNAL_INDEX_COLUMNS = ("type", "crm", "local_id", "uri")

HARVEST_LOG_PATTERNS = {
    "collections": re.compile(r"Collections visited:\s*(\d+)"),
    "resources": re.compile(r"Resources visited:\s*(\d+)"),
    "citable_units": re.compile(r"CitableUnits visited:\s*(\d+)"),
}


@dataclass
class CountCheck:
    label: str
    expected: int | None
    actual: int
    ok: bool
    detail: str = ""


@dataclass
class SetCheck:
    label: str
    missing: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    ok: bool = True

    @property
    def missing_count(self) -> int:
        return len(self.missing)


@dataclass
class PairCheck:
    label: str
    missing_pairs: list[tuple[str, str]] = field(default_factory=list)
    ok: bool = True

    @property
    def missing_count(self) -> int:
        return len(self.missing_pairs)


@dataclass
class VerificationReport:
    harvest_counts: list[CountCheck] = field(default_factory=list)
    entity_register: SetCheck | None = None
    index_citable_units: SetCheck | None = None
    entity_passage_pairs: PairCheck | None = None

    @property
    def ok(self) -> bool:
        if any(not check.ok for check in self.harvest_counts):
            return False
        for check in (self.entity_register, self.index_citable_units, self.entity_passage_pairs):
            if check is not None and not check.ok:
                return False
        return True


def configure_logging(level: str, log_file: str | None) -> None:
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    if log_file:
        file_handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
        )
        root_logger.addHandler(file_handler)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify HWGW DTS pipeline data integrity across transformation steps."
    )
    parser.add_argument(
        "--harvest-ttl",
        required=True,
        help="Harvested Turtle file (from harvest_jsonld_to_ttl.py).",
    )
    parser.add_argument(
        "--harvest-log",
        default=None,
        help="Harvest log file with visited counts (optional but recommended).",
    )
    parser.add_argument(
        "--entities-passages",
        required=True,
        help="Entities–passages CSV (from build_entities_passages_index.py).",
    )
    parser.add_argument(
        "--external-index",
        required=True,
        help="External entity register CSV (register-entity-index.csv).",
    )
    parser.add_argument(
        "--combined-ttl",
        required=True,
        help="Combined Turtle file (from combine_entity_graph.py).",
    )
    parser.add_argument(
        "--sample-limit",
        type=int,
        default=10,
        help="Max missing items to print per check (default: 10).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level.",
    )
    parser.add_argument(
        "--log-file",
        default="data/logs/verify_pipeline.log",
        help="Optional log file path.",
    )
    return parser


def load_graph(path: str | Path) -> Graph:
    graph = Graph()
    graph.parse(path, format="turtle")
    LOGGER.info("Loaded %s triples from %s", len(graph), path)
    return graph


def count_dts_entities(graph: Graph) -> dict[str, int]:
    collections = len(list(graph.subjects(RDF.type, DTS.Collection)))
    resources = len(list(graph.subjects(RDF.type, DTS.Resource)))
    citable_units = len(list(graph.subjects(RDF.type, DTS.CitableUnit)))
    return {
        "collections": collections,
        "resources": resources,
        "citable_units": citable_units,
    }


def parse_harvest_log(path: Path) -> dict[str, int]:
    text = path.read_text(encoding="utf-8")
    parsed: dict[str, int] = {}
    for key, pattern in HARVEST_LOG_PATTERNS.items():
        matches = [int(value) for value in pattern.findall(text)]
        if not matches:
            raise ValueError(f"Harvest log {path} is missing '{key}' count.")
        parsed[key] = matches[-1]
    return parsed


def load_entities_passages(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV file has no header row: {path}")
        missing = [col for col in ENTITIES_PASSAGES_COLUMNS if col not in reader.fieldnames]
        if missing:
            raise ValueError(f"Entities–passages CSV missing columns: {', '.join(missing)}")
        return [{key: str(row.get(key) or "") for key in ENTITIES_PASSAGES_COLUMNS} for row in reader]


def load_external_index(path: Path) -> dict[str, str]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV file has no header row: {path}")
        missing = [col for col in EXTERNAL_INDEX_COLUMNS if col not in reader.fieldnames]
        if missing:
            raise ValueError(f"External index CSV missing columns: {', '.join(missing)}")
        indexed: dict[str, str] = {}
        for row in reader:
            local_id = str(row.get("local_id") or "").strip()
            uri = str(row.get("uri") or "").strip()
            if local_id and uri:
                indexed[local_id] = uri
        return indexed


def citable_unit_identifiers(graph: Graph) -> set[str]:
    identifiers: set[str] = set()
    for subject in graph.subjects(RDF.type, DTS.CitableUnit):
        identifier = graph.value(subject, DTS.identifier)
        if identifier is not None:
            identifiers.add(str(identifier))
    return identifiers


def verify_harvest_counts(
    harvest_ttl: Path,
    harvest_log: Path | None,
) -> list[CountCheck]:
    graph = load_graph(harvest_ttl)
    actual = count_dts_entities(graph)
    if harvest_log is None:
        LOGGER.warning("No harvest log provided; skipping harvest count verification.")
        return [
            CountCheck(label=key, expected=None, actual=value, ok=True, detail="log not provided")
            for key, value in actual.items()
        ]

    expected = parse_harvest_log(harvest_log)
    checks: list[CountCheck] = []
    labels = {
        "collections": "Collections",
        "resources": "Resources (documents)",
        "citable_units": "CitableUnits",
    }
    for key, label in labels.items():
        exp = expected[key]
        act = actual[key]
        checks.append(
            CountCheck(
                label=label,
                expected=exp,
                actual=act,
                ok=exp == act,
                detail=f"expected {exp}, got {act}",
            )
        )
    return checks


def verify_entity_register(
    passage_rows: list[dict[str, str]],
    external_index: dict[str, str],
) -> SetCheck:
    rs_refs = sorted({row["rs_ref"].strip() for row in passage_rows if row["rs_ref"].strip()})
    missing = [rs_ref for rs_ref in rs_refs if rs_ref not in external_index]
    return SetCheck(
        label="entities–passages rs_ref in register-entity-index",
        missing=missing,
        ok=not missing,
    )


def verify_index_citable_units(
    passage_rows: list[dict[str, str]],
    harvest_graph: Graph,
) -> SetCheck:
    unit_ids = sorted({row["citable_unit_id"].strip() for row in passage_rows if row["citable_unit_id"].strip()})
    harvested = citable_unit_identifiers(harvest_graph)
    missing = [unit_id for unit_id in unit_ids if unit_id not in harvested]
    return SetCheck(
        label="index citable_unit_id in harvest TTL",
        missing=missing,
        ok=not missing,
    )


def verify_entity_passage_pairs(
    passage_rows: list[dict[str, str]],
    external_index: dict[str, str],
    combined_graph: Graph,
) -> PairCheck:
    missing_pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for row in passage_rows:
        unit_id = row["citable_unit_id"].strip()
        rs_ref = row["rs_ref"].strip()
        if not unit_id or not rs_ref:
            continue
        entity_uri = external_index.get(rs_ref)
        if entity_uri is None:
            continue

        pair = (unit_id, entity_uri)
        if pair in seen:
            continue
        seen.add(pair)

        digital_object = URIRef(f"{HWGW_DTS_BASE}{unit_id}")
        if (digital_object, CRM.P67_refers_to, URIRef(entity_uri)) not in combined_graph:
            missing_pairs.append(pair)

    return PairCheck(
        label="entity–passage pairs in combined TTL (D1 crm:P67_refers_to entity URI)",
        missing_pairs=sorted(missing_pairs),
        ok=not missing_pairs,
    )


def run_verification(
    harvest_ttl: Path,
    harvest_log: Path | None,
    entities_passages: Path,
    external_index_path: Path,
    combined_ttl: Path,
) -> VerificationReport:
    report = VerificationReport()
    report.harvest_counts = verify_harvest_counts(harvest_ttl, harvest_log)

    passage_rows = load_entities_passages(entities_passages)
    external_index = load_external_index(external_index_path)
    harvest_graph = load_graph(harvest_ttl)
    combined_graph = load_graph(combined_ttl)

    report.entity_register = verify_entity_register(passage_rows, external_index)
    report.index_citable_units = verify_index_citable_units(passage_rows, harvest_graph)
    report.entity_passage_pairs = verify_entity_passage_pairs(
        passage_rows, external_index, combined_graph
    )
    return report


def _print_sample(items: list[str], limit: int) -> None:
    for item in items[:limit]:
        print(f"  - {item}")
    remaining = len(items) - limit
    if remaining > 0:
        print(f"  ... and {remaining} more")


def print_report(report: VerificationReport, sample_limit: int) -> None:
    print("Pipeline verification report")
    print("=" * 40)

    if report.harvest_counts:
        print("\nHarvest TTL vs harvest log:")
        for check in report.harvest_counts:
            if check.expected is None:
                print(f"  [skip] {check.label}: {check.actual} in TTL ({check.detail})")
            else:
                status = "OK" if check.ok else "FAIL"
                print(f"  [{status}] {check.label}: {check.detail}")

    if report.entity_register is not None:
        status = "OK" if report.entity_register.ok else "FAIL"
        print(f"\n[{status}] {report.entity_register.label}")
        if report.entity_register.missing:
            print(f"  Missing rs_ref ({report.entity_register.missing_count}):")
            _print_sample(report.entity_register.missing, sample_limit)

    if report.index_citable_units is not None:
        status = "OK" if report.index_citable_units.ok else "FAIL"
        print(f"\n[{status}] {report.index_citable_units.label}")
        if report.index_citable_units.missing:
            print(f"  Missing citable_unit_id ({report.index_citable_units.missing_count}):")
            _print_sample(report.index_citable_units.missing, sample_limit)

    if report.entity_passage_pairs is not None:
        status = "OK" if report.entity_passage_pairs.ok else "FAIL"
        print(f"\n[{status}] {report.entity_passage_pairs.label}")
        if report.entity_passage_pairs.missing_pairs:
            print(f"  Missing pairs ({report.entity_passage_pairs.missing_count}):")
            pair_samples = [
                f"{unit_id} → {entity_uri}"
                for unit_id, entity_uri in report.entity_passage_pairs.missing_pairs
            ]
            _print_sample(pair_samples, sample_limit)

    print("\n" + ("All checks passed." if report.ok else "Verification failed."))


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    configure_logging(args.log_level, args.log_file)

    harvest_log = Path(args.harvest_log) if args.harvest_log else None
    if harvest_log is not None and not harvest_log.is_file():
        LOGGER.warning("Harvest log not found (%s); skipping harvest count verification.", harvest_log)
        harvest_log = None

    report = run_verification(
        harvest_ttl=Path(args.harvest_ttl),
        harvest_log=harvest_log,
        entities_passages=Path(args.entities_passages),
        external_index_path=Path(args.external_index),
        combined_ttl=Path(args.combined_ttl),
    )

    print_report(report, sample_limit=args.sample_limit)
    LOGGER.info("Verification result: %s", "passed" if report.ok else "failed")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
