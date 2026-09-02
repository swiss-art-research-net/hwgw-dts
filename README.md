# HWGW DTS pipeline

Scripts for harvesting a DTS API as RDF, indexing TEI entity mentions, and
linking them to the HWGW knowledge graph.

## Pipeline

```
DTS API  ──►  harvest_jsonld_to_ttl.py  ──►  data/harvest/*.ttl
                                                    │
TEI/DTS  ──►  build_entities_passages_index.py  ──►  data/index/*.csv
                                                    │
register-entity-index.csv (external)  ──────────────┤
                                                    ▼
                              combine_entity_graph.py  ──►  data/combined/*.ttl
```

### Data layout

| Directory | Contents |
|---|---|
| `data/harvest/` | Turtle graphs harvested from DTS |
| `data/index/` | Entities–passages CSV indices |
| `data/external/` | Third-party inputs (e.g. `register-entity-index.csv`) |
| `data/combined/` | Enriched RDF output (generated) |
| `data/logs/` | Script log files (generated) |

Download the entity register from
[hwgw-pipeline/mapping/register-entity-index.csv](https://github.com/swiss-art-research-net/hwgw-pipeline/blob/x3ml-mapping/mapping/register-entity-index.csv)
into `data/external/`.

## Install

```bash
python -m pip install -r requirements.txt
```

Or use [just](https://github.com/casey/just) for common tasks (`just --list`).

```bash
just install        # runtime deps
just install-dev    # + pytest
just test
```

Development dependencies and tests:

```bash
python -m pip install -r requirements-dev.txt
pytest
```

### Just recipes

| Recipe | Description |
|---|---|
| `just harvest` | Full DTS crawl → `data/harvest/merged.ttl` |
| `just harvest-sample` | Quick crawl (20 resources) |
| `just index` | Build entities–passages CSV for s03/ed |
| `just fetch-external-index` | Download entity register (needs `gh`) |
| `just combine` | Enrich RDF from harvest + index + register |
| `just clean` | Remove generated outputs (logs, combined, external) |
| `just pipeline` | `fetch-external-index` → `index` → `combine` |
| `just pipeline-all` | Full harvest + pipeline |
| `just pipeline-quick` | Sample harvest + capped index + combine |
| `just pipeline-hwgw` | HWGW collection subset pipeline (see below) |
| `just verify` | Check data integrity across pipeline outputs |

Override paths via positional recipe arguments, e.g.
`just combine data/harvest/merged.ttl` or `just verify data/harvest/hwgw.ttl`.

### HWGW subset recipes

These recipes target the HWGW collection subtree on the RS4 DTS endpoint
(`https://example.org/dts/collections/hwgw`, volumes s01, s03, s04, …) rather
than the full RS4 crawl. They are the usual entry point for working on HWGW
edition data.

| Recipe | Description |
|---|---|
| `just harvest-hwgw` | Harvest HWGW collection → `data/harvest/hwgw.ttl` |
| `just index-hwgw` | Build entities–passages index for the HWGW collection |
| `just combine-hwgw` | Combine HWGW harvest + index + register |
| `just pipeline-hwgw` | End-to-end: `harvest-hwgw` → `fetch-external-index` → `index` → combine |
| `just verify-hwgw` | Verify integrity using HWGW harvest and combined outputs |

Default HWGW paths (defined at the top of the `justfile`):

| Variable | Path |
|---|---|
| Harvest TTL | `data/harvest/hwgw.ttl` |
| Harvest log | `data/logs/harvest-hwgw.log` |
| Entities–passages index | `data/index/hwgw_entities_passages.csv` |
| Combined RDF | `data/combined/hwgw_combined.ttl` |

Run the full HWGW pipeline:

```bash
just pipeline-hwgw
```

Then verify and extract a document sample:

```bash
just verify-hwgw
just document-sample combined=data/combined/hwgw_combined.ttl unit_id=s03-pg85
```

The default `just index` recipe indexes the s03/ed resource
(`published_page` / `page` cite type) into the same
`hwgw_entities_passages.csv` used by the HWGW combine step; `pipeline-hwgw`
calls that recipe rather than `index-hwgw`.

## 1. Harvest JSON-LD

Connects to a DTS API, crawls collections/resources/navigation, and merges
parseable JSON-LD into one Turtle file.

```bash
python scripts/harvest_jsonld_to_ttl.py \
  --entrypoint "http://rs4.ethz.ch/dts/" \
  --output "data/harvest/rs4_merged.ttl"
```

Quick test with a resource cap:

```bash
python scripts/harvest_jsonld_to_ttl.py \
  --output "data/harvest/rs4_sample.ttl" \
  --max-resources 20
```

Limit to one collection subtree:

```bash
python scripts/harvest_jsonld_to_ttl.py \
  --collection-id "https://example.org/dts/collections/hwgw/s03" \
  --output "data/harvest/rs4_s03.ttl"
```

The crawl shows a live `tqdm` progress bar. Logging goes to `data/logs/harvest.log`.

Harvested Turtle expands DTS endpoint URI templates into resolvable IRIs. For
each `dts:Resource`, the harvester emits one `dts:navigation` link per declared
`CitationTree`, with explicit `tree` and `down=-1` parameters.

## 2. Build entities–passages index

Build a CSV index of TEI `<rs>` mentions linked to DTS CitableUnits for one
Resource and CitationTree:

```bash
python scripts/build_entities_passages_index.py \
  --resource-id "https://example.org/dts/collections/hwgw/s03/ed" \
  --tree "published_page" \
  --cite-type "page" \
  --output "data/index/s03_ed_entities_passages.csv"
```

Useful options:

- `--tree`: CitationTree identifier (`logical_structure`, `published_page`, …).
  Defaults to the first tree declared on the resource.
- `--cite-type`: only index units of that citeType (e.g. `paragraph`, `page`).
- `--leaf-only`: only process deepest-level units in the selected tree.
- `--max-units`: cap processed units for quick tests.

CSV columns: `rs_type`, `rs_ref`, `rs_text`, `citation_tree`,
`citable_unit_cite_type`, `citable_unit_id`, `document_uri`.

## 3. Combine into enriched RDF

Join a harvested TTL graph with an entities–passages index and the external
entity register. Existing `dts:CitableUnit` blank nodes in the harvest are
linked via CRM/CRMdig to HWGW entity URIs.

```bash
python scripts/combine_entity_graph.py \
  --ttl data/harvest/rs4_sample_fresh.ttl \
  --entities-passages data/index/s03_ed_entities_passages.csv \
  --external-index data/external/register-entity-index.csv \
  --output data/combined/s03_ed_combined.ttl
```

The `--ttl` input must include navigation/CitableUnits (use a full harvest, not
a collection-only subset).

External index CSV columns: `type`, `crm`, `local_id`, `uri`. Rows are joined
on `entities_passages.rs_ref = external_index.local_id`.

## Document sample

Extract a self-contained Turtle subgraph for one CitableUnit (for review or
sharing):

```bash
just combine
just document-sample unit_id=s03-pg85
```

Output: `data/samples/s03-pg85_document.ttl` — DTS Resource/Navigation,
CRM/CRMdig enrichment, and `crm:P67_refers_to` links to HWGW entities.

## Verify pipeline integrity

Check that harvest counts match the TTL export, entities in the passages
index appear in the register, and entity–passage pairs are present in the
combined graph:

```bash
just combine
just verify
```

Or explicitly:

```bash
python scripts/verify_pipeline.py \
  --harvest-ttl data/harvest/rs4_sample_fresh.ttl \
  --harvest-log data/logs/harvest.log \
  --entities-passages data/index/s03_ed_entities_passages.csv \
  --external-index data/external/register-entity-index.csv \
  --combined-ttl data/combined/s03_ed_combined.ttl
```

The script exits with code 1 when any check fails.

## Notes

- Scripts default to `http://rs4.ethz.ch/dts/`.
- They use the `DTS_API` client pattern from the DTS validator.
