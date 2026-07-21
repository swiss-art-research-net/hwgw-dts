# DTS JSON-LD Harvester

This repository contains a script that:

1. Connects to a DTS API endpoint.
2. Recursively traverses collections/resources using the DTS validator client.
3. For each discovered resource, queries the navigation endpoint.
4. Traverses CitationTrees and crawls reachable CitableUnits.
5. Extracts parseable JSON-LD objects.
6. Merges everything into one RDF graph.
7. Writes the graph as a Turtle file.

## Install

```bash
python -m pip install -r requirements.txt
```

## Usage

Prototype endpoint (provided):

```bash
python scripts/harvest_jsonld_to_ttl.py \
  --entrypoint "http://rs4.ethz.ch/dts/" \
  --output "rs4_merged.ttl" \
  --log-file "harvest.log"
```

Optional limit for quick tests:

```bash
python scripts/harvest_jsonld_to_ttl.py \
  --entrypoint "http://rs4.ethz.ch/dts/" \
  --output "rs4_sample.ttl" \
  --max-resources 20
```

Limit crawling to a single collection subtree:

```bash
python scripts/harvest_jsonld_to_ttl.py \
  --entrypoint "http://rs4.ethz.ch/dts/" \
  --collection-id "https://example.org/dts/collections/hwgw/s03" \
  --output "rs4_s03.ttl"
```

The crawl shows a live `tqdm` progress bar in terminal. Logging is written to
`harvest.log` (or the path provided via `--log-file`) to keep stdout clean.

Harvested Turtle expands DTS endpoint URI templates into resolvable IRIs. For
each `dts:Resource`, the harvester emits one `dts:navigation` link per declared
`CitationTree`, with explicit `tree` and `down=-1` parameters, so Resources can
be linked to their corresponding `dts:Navigation` nodes in the merged graph.

Install the development dependencies and run the regression tests with:

```bash
python -m pip install -r requirements-dev.txt
pytest
```

## Entities–passages index

Build a CSV index of TEI `<rs>` mentions linked to DTS CitableUnits for one
Resource and CitationTree:

```bash
python scripts/build_entities_passages_index.py \
  --entrypoint "http://rs4.ethz.ch/dts/" \
  --resource-id "https://example.org/dts/collections/hwgw/s03/ed" \
  --tree "logical_structure" \
  --cite-type "paragraph" \
  --output "data/output/s03_ed_entities_passages.csv" \
  --log-file "data/entities_index.log"
```

Useful options:

- `--tree`: CitationTree identifier (`logical_structure`, `published_page`, …).
  Defaults to the first tree declared on the resource.
- `--cite-type`: only index units of that citeType (e.g. `paragraph`).
- `--leaf-only`: only process deepest-level units in the selected tree.
- `--max-units`: cap processed units for quick tests.

CSV columns: `rs_type`, `rs_ref`, `rs_text`, `citation_tree`,
`citable_unit_cite_type`, `citable_unit_id`, `document_uri`.

## Notes

- The scripts default to `http://rs4.ethz.ch/dts/`.
- They use the `DTS_API` client pattern shown in the validator example notebook.
