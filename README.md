# babelfish_aep_query_renamer

A small command-line tool that tidies up SQL query templates in Adobe
Experience Platform's Query Service (sometimes nicknamed *Babelfish*) by
pulling them down, suggesting sensible names — optionally via the Claude
API — and pushing the renames back.

## Why

Babelfish makes it easy to fire off a lot of queries during exploration and
iterative development. There's no enforced naming convention, so over a few
weeks of work the Templates panel ends up full of `33333`, `xxxxx`,
`testsite_c - select all`, half-finished experiments, and several "v2"s of
the same idea. Across multiple sandboxes and orgs it gets even messier. This
script is a one-shot tidy-up: it lists every template you own, proposes a
clean name for each (Claude reads the SQL and suggests a kebab-case name
tagged `[babelfish]` so you can always tell which were AI-renamed), and lets
you accept, edit, or skip per-template — or run the whole thing in batch
mode.

It's stdlib-only (no `pip install` needed, friendly to locked-down VDIs),
config-driven via a single `config.json`, and tenant-aware so the same script
runs cleanly across multiple Adobe orgs (e.g. an internal sandbox plus a
client org) without folder collisions. Each run also writes a snapshot and
rebuilds a single cross-tenant Markdown mega-file with every query's SQL —
useful as a documentation export or as input to an LLM analysing the whole
estate.

## Quick start

1. `cp config.example.json config.json` and fill in your Adobe IMS
   `client_id`, `client_secret`, `org_id`, and `sandbox_names`.
2. Optionally paste an `anthropic_api_key` to enable AI-suggested names.
3. `python babelfish_query_renamer.py`

`config.json` is gitignored — never commit it.

---

# batch_fetcher_2

A small command-line tool that pulls a single AEP batch's files down to your
local machine. It lists the most recent batches in the sandbox, you paste the
batch ID you want, and the script downloads every file in that batch.

## Why

When investigating a specific ingest — a failed run, a sample export, a
suspicious record count — the AEP UI makes it awkward to actually grab the
underlying file. This script does it in one shot from a VDI: authenticate,
show the last 24 hours of batches with their status and record counts, prompt
for an ID, and write the file(s) to the current directory. It replaces three
earlier scripts (`auth.py`, `authandret.py`, `fetchbatch.py`) — same flow,
one file, hooked into the shared `config.json`.

Stdlib-only (no `pip install` needed, friendly to locked-down VDIs) and
config-driven, so it picks up the same credentials as the Babelfish renamer.
GBR9 region headers are sent by default (override via `region` in
`config.json` if you're elsewhere).

## Quick start

1. `cp config.example.json config.json` and fill in your Adobe IMS
   `client_id`, `client_secret`, `org_id`, and `sandbox_names`.
2. `python batch_fetcher_2.py`
3. At the prompt, paste a batch ID (or pick one from the listed table) and
   hit enter.

`config.json` is gitignored — never commit it.
