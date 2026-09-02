# HWGW DTS pipeline — run `just` or `just --list` to see recipes.

entrypoint := "http://rs4.ethz.ch/dts/"
resource_id := "https://example.org/dts/collections/hwgw/s03/ed"
tree := "published_page"
cite_type := "page"

harvest_ttl := "data/harvest/rs4_sample_fresh.ttl"

harvest_log := "data/logs/harvest.log"
index_csv := "data/index/hwgw_entities_passages.csv"
external_index := "data/external/register-entity-index.csv"
combined_ttl := "data/combined/s03_ed_combined.ttl"
external_index_repo := "swiss-art-research-net/hwgw-pipeline"
external_index_path := "mapping/register-entity-index.csv"
external_index_ref := "x3ml-mapping"

hwgw_collection_id := "https://example.org/dts/collections/hwgw"
hwgw_harvest_ttl := "data/harvest/hwgw.ttl"
hwgw_harvest_log := "data/logs/harvest-hwgw.log"
hwgw_combined_ttl := "data/combined/hwgw_combined.ttl"

default:
    @just --list

# ── Setup ────────────────────────────────────────────────────────────────────

dirs:
    mkdir -p data/harvest data/index data/external data/combined data/logs

install:
    python -m pip install -r requirements.txt

install-dev:
    python -m pip install -r requirements-dev.txt

test:
    pytest

# Verify pipeline data integrity across harvest, index, and combine steps.
verify harvest=harvest_ttl combined=combined_ttl: dirs
    python scripts/verify_pipeline.py \
        --harvest-ttl "{{harvest}}" \
        --harvest-log "{{harvest_log}}" \
        --entities-passages "{{index_csv}}" \
        --external-index "{{external_index}}" \
        --combined-ttl "{{combined}}"

# Verify pipeline data integrity across harvest, index, and combine steps.
verify-hwgw harvest=hwgw_harvest_ttl combined=hwgw_combined_ttl: dirs
    python scripts/verify_pipeline.py \
        --harvest-ttl "{{hwgw_harvest_ttl}}" \
        --harvest-log "{{hwgw_harvest_log}}" \
        --entities-passages "{{index_csv}}" \
        --external-index "{{external_index}}" \
        --combined-ttl "{{combined}}"

# Remove generated outputs (logs, combined RDF, external downloads).
# Committed samples in data/harvest/ and data/index/ are kept.
clean:
    #!/usr/bin/env bash
    set -euo pipefail
    rm -rf data/logs/* data/combined/* data/external/*
    rm -f data/harvest/merged.ttl
    mkdir -p data/logs data/combined data/external
    echo "Cleaned data/logs/, data/combined/, data/external/, and data/harvest/merged.ttl"

# ── Step 1: Harvest JSON-LD ──────────────────────────────────────────────────

# Full crawl (can take a while).
harvest: dirs
    python scripts/harvest_jsonld_to_ttl.py \
        --entrypoint "{{entrypoint}}" \
        --output data/harvest/merged.ttl \
        --log-file "{{harvest_log}}"

# Harvest the HWGW collection subtree (s01, s03, s04, …).
harvest-hwgw: dirs
    python scripts/harvest_jsonld_to_ttl.py \
        --entrypoint "{{entrypoint}}" \
        --collection-id "{{hwgw_collection_id}}" \
        --output "{{hwgw_harvest_ttl}}" \
        --log-file "{{hwgw_harvest_log}}"

# Quick harvest for smoke tests (20 resources).
harvest-sample: dirs
    python scripts/harvest_jsonld_to_ttl.py \
        --entrypoint "{{entrypoint}}" \
        --output data/harvest/rs4_sample.ttl \
        --max-resources 20

# Harvest a single collection subtree (no CitableUnits — not suitable for combine).
harvest-s03: dirs
    python scripts/harvest_jsonld_to_ttl.py \
        --entrypoint "{{entrypoint}}" \
        --collection-id "https://example.org/dts/collections/hwgw/s03" \
        --output data/harvest/rs4_s03.ttl

# ── Step 2: Entities–passages index ──────────────────────────────────────────

index: dirs
    python scripts/build_entities_passages_index.py \
        --entrypoint "{{entrypoint}}" \
        --resource-id "{{resource_id}}" \
        --tree "{{tree}}" \
        --cite-type "{{cite_type}}" \
        --output "{{index_csv}}"

index-hwgw: dirs
    python scripts/build_entities_passages_index.py \
        --entrypoint "{{entrypoint}}" \
        --resource-id "{{hwgw_collection_id}}" \
        --tree "{{tree}}" \
        --cite-type "{{cite_type}}" \
        --output "{{index_csv}}"

# Cap processed units for a quick index run.
index-sample max_units='10': dirs
    python scripts/build_entities_passages_index.py \
        --entrypoint "{{entrypoint}}" \
        --resource-id "{{resource_id}}" \
        --tree "{{tree}}" \
        --cite-type "{{cite_type}}" \
        --max-units {{max_units}} \
        --output "{{index_csv}}"

# ── External entity register ─────────────────────────────────────────────────

# Download register-entity-index.csv from hwgw-pipeline (requires `gh` CLI).
fetch-external-index: dirs
    #!/usr/bin/env bash
    set -euo pipefail
    gh api \
        "repos/{{external_index_repo}}/contents/{{external_index_path}}?ref={{external_index_ref}}" \
        --jq .content | tr -d '\n' | base64 -d > "{{external_index}}"
    echo "Wrote {{external_index}} ($(wc -l < "{{external_index}}") lines)"

# ── Step 3: Combine into enriched RDF ────────────────────────────────────────

combine harvest=harvest_ttl: dirs
    python scripts/combine_entity_graph.py \
        --ttl "{{harvest}}" \
        --entities-passages "{{index_csv}}" \
        --external-index "{{external_index}}" \
        --output "{{combined_ttl}}"

combine-hwgw harvest=hwgw_harvest_ttl: dirs
    python scripts/combine_entity_graph.py \
        --ttl "{{hwgw_harvest_ttl}}" \
        --entities-passages "{{index_csv}}" \
        --external-index "{{external_index}}" \
        --output "{{hwgw_combined_ttl}}"

# ── Pipelines ────────────────────────────────────────────────────────────────

# End-to-end using an existing harvest (default: committed rs4_sample_fresh.ttl).
pipeline: fetch-external-index index combine

# End-to-end including a fresh full harvest.
pipeline-all: harvest fetch-external-index index
    @just combine data/harvest/merged.ttl

# Quick smoke test: sample harvest + capped index + combine.
pipeline-quick: harvest-sample index-sample
    @just combine data/harvest/rs4_sample.ttl

# End-to-end for the HWGW collection subset (harvest → index s03/ed → combine).
pipeline-hwgw: harvest-hwgw fetch-external-index index
    @just combine {{hwgw_harvest_ttl}}

# Extract a single-document Turtle sample from the combined graph.
document-sample unit_id="s03-pg85" combined="data/combined/s03_ed_combined.ttl": dirs
    python scripts/extract_document_sample.py \
        --input "{{combined}}" \
        --unit-id "{{unit_id}}" \
        --output "data/samples/{{unit_id}}_document.ttl"
