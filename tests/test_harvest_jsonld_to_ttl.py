from __future__ import annotations

import copy
import sys
from pathlib import Path

from rdflib import Graph

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from harvest_jsonld_to_ttl import (  # noqa: E402
    HarvestStats,
    add_jsonld_from_object,
    normalize_endpoint_links,
)


# Inline context keeps tests offline (no remote @context fetch).
INLINE_CONTEXT = {
    "dts": "https://dtsapi.org/v1.0#",
    "Resource": "dts:Resource",
    "CitationTree": "dts:CitationTree",
    "citationTrees": "dts:citationTrees",
    "citeType": "dts:citeType",
    "identifier": "dts:identifier",
    "collection": {"@id": "dts:collection", "@type": "@id"},
    "document": {"@id": "dts:document", "@type": "@id"},
    "navigation": {"@id": "dts:navigation", "@type": "@id"},
    "title": "dts:title",
}

SAMPLE_RESOURCE = {
    "@context": INLINE_CONTEXT,
    "@type": "Resource",
    "@id": "https://example.org/dts/collections/hwgw/s03/ed",
    "title": "Salomon Geßner",
    "citationTrees": [
        {
            "@type": "CitationTree",
            "identifier": "logical_structure",
            "citeType": "chapter",
        },
        {
            "@type": "CitationTree",
            "identifier": "published_page",
            "citeType": "page",
        },
    ],
    "collection": (
        "http://rs4.ethz.ch/dts/collection/?id=https%3A%2F%2Fexample.org%2Fdts%2Fcollections%2Fhwgw%2Fs03%2Fed{&nav}"
    ),
    "document": (
        "http://rs4.ethz.ch/dts/document/?resource=https%3A%2F%2Fexample.org%2Fdts%2Fcollections%2Fhwgw%2Fs03%2Fed{&ref,start,end,tree}"
    ),
    "navigation": (
        "http://rs4.ethz.ch/dts/navigation/?resource=https%3A%2F%2Fexample.org%2Fdts%2Fcollections%2Fhwgw%2Fs03%2Fed{&ref,start,end,tree,down}"
    ),
}


def test_resource_navigation_links_per_citation_tree() -> None:
    normalized = normalize_endpoint_links(copy.deepcopy(SAMPLE_RESOURCE))
    navigation = normalized["navigation"]
    assert isinstance(navigation, list)
    assert len(navigation) == 2
    uris = [link["@id"] for link in navigation]
    assert all("down=-1" in uri for uri in uris)
    assert "tree=logical_structure" in uris[0]
    assert "tree=published_page" in uris[1]
    assert "{" not in "".join(uris)


def test_collection_and_document_templates_are_expanded() -> None:
    normalized = normalize_endpoint_links(copy.deepcopy(SAMPLE_RESOURCE))
    collection_uri = normalized["collection"]["@id"]
    document_uri = normalized["document"]["@id"]
    assert "{" not in collection_uri
    assert "{" not in document_uri
    assert "collection/?id=" in collection_uri
    assert "document/?resource=" in document_uri


def test_nested_resource_is_normalized() -> None:
    payload = {
        "@type": "Navigation",
        "@id": "http://example.org/navigation",
        "resource": copy.deepcopy(SAMPLE_RESOURCE),
    }
    normalized = normalize_endpoint_links(payload)
    resource = normalized["resource"]
    assert len(resource["navigation"]) == 2
    assert "{" not in resource["collection"]["@id"]


def test_duplicate_citation_trees_produce_single_navigation_links() -> None:
    payload = copy.deepcopy(SAMPLE_RESOURCE)
    payload["citationTrees"] = payload["citationTrees"] + [dict(payload["citationTrees"][0])]
    graph = Graph()
    stats = HarvestStats()
    seen_jsonld_signatures: set[str] = set()
    seen_resource_citation_tree_ids: dict[str, set[str]] = {}
    add_jsonld_from_object(
        payload,
        graph,
        stats,
        seen_jsonld_signatures,
        seen_resource_citation_tree_ids,
    )
    turtle = graph.serialize(format="turtle")
    assert turtle.count("tree=logical_structure&down=-1") == 1
    assert turtle.count("tree=published_page&down=-1") == 1


def test_endpoint_values_serialize_as_iris() -> None:
    graph = Graph()
    stats = HarvestStats()
    seen_jsonld_signatures: set[str] = set()
    seen_resource_citation_tree_ids: dict[str, set[str]] = {}
    add_jsonld_from_object(
        SAMPLE_RESOURCE,
        graph,
        stats,
        seen_jsonld_signatures,
        seen_resource_citation_tree_ids,
    )
    turtle = graph.serialize(format="turtle")
    assert "dts:collection <http://rs4.ethz.ch/dts/collection/?id=" in turtle
    assert "dts:document <http://rs4.ethz.ch/dts/document/?resource=" in turtle
    assert "tree=logical_structure&down=-1>" in turtle
    assert "tree=published_page&down=-1>" in turtle
    assert "{&nav}" not in turtle
    assert "{&ref,start,end,tree,down}" not in turtle
