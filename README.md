# DTS JSON-LD Harvester

This repository contains a script that:

1. Connects to a DTS API endpoint.
2. Recursively traverses collections/resources using the DTS validator client.
3. Extracts parseable JSON-LD objects.
4. Merges everything into one RDF graph.
5. Writes the graph as a Turtle file.

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

## Notes

- The script defaults to `http://rs4.ethz.ch/dts/`.
- It uses the `DTS_API` client pattern shown in the validator example notebook.
