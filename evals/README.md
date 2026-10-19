# Source-grounding evaluation

Run from the repository root with Python 3.9+ and no installed dependencies:

```sh
python -m evals.run --output evals/latest.json
python -m unittest discover -s tests
```

This evaluates the actual `python -m agent_brain` CLI, not prerecorded answers or a
mock implementation. Each scenario creates an isolated temporary fixture, invokes
the CLI with an explicit `--brain`, records the outputs, and deletes the fixture.
It never reads your configured brain, agent transcripts, accounts, or project data.
The corpus is handwritten synthetic data, built around the product's documented
failure modes. No private customer or application material is included.

## Rubric: source-grounding-v1

Each case passes only if every assertion passes. The acceptance gate requires all
cases to pass; a good average cannot hide leaked data or fabricated evidence.

| Dimension | Observable acceptance criteria |
| --- | --- |
| Retrieval | Current corrections outrank history; misses return no results; pagination gives a usable continuation |
| Provenance | Readback matches the cited line; invented quotes are rejected; source edits flag evidence drift |
| Isolation | Searches exclude another project; cross-project reads fail; similar folder names stay unmapped |
| Continuity | Updates preserve unresolved work and constraints; implemented work does not become tested or released work |

`cases.json` stores explicit inputs and expected observations. Assertions use JSON
paths and `equals`, `contains`, or `excludes`. Missing fields, wrong types, unknown
operators, CLI failures and timeouts fail the case. Setup steps must also pass their
checks. IDs from successful saves can be referenced by later steps, so the corpus
tests real supersession chains rather than guessing file names.
The `$json_lines` path component parses the CLI's returned source lines as JSON,
so state assertions inspect persisted readback rather than the submitted input.

The JSON report records each invocation and its observed output, individual failed
assertions, per-dimension counts, corpus SHA-256, engine SHA-256, and evaluator
SHA-256 to identify the exact inputs and scoring implementation. Temporary paths
and generated handoff IDs become fixture variables. Timestamps and serialized
handoff hashes remain observations, so reports are auditable but not byte-identical
across runs. `evals/latest.json` is local run output and is not committed.

[reference-report.json](reference-report.json) is one actual local run: 12 of 12
cases passed, three in each dimension. Its hashes identify the evaluated source,
corpus and scorer. Re-run the command above to check your own revision.

## Check the evaluator

`tests/test_evals.py` deliberately submits fabricated retrieval output, missing
evidence, wrong field types, an unknown operator and a crashing driver. It also
checks malformed corpora and rejects fixture writes outside the temporary folder.
These controls test whether a bad candidate can get a passing score. The same test
module runs the full corpus through the real CLI.
An additional mutation control runs the real CLI and deliberately places history
before current evidence; the ranking case must fail. Commands that could install
hooks or sync a repository, and attempts to override the temporary brain, are refused.

To compare a code change, run the suite on each revision using the same corpus and
compare failed case IDs and dimension counts. Preserve both reports outside the
checkout. Change the rubric version when changing acceptance rules, and retain
case IDs for regressions. Add cases based on observed failures before fixing them.

## Scope

This is a deterministic contract evaluation for retrieval and evidence handling.
It does not measure an LLM's answer quality, semantic relevance, latency at scale,
human preference, or truth beyond matching registered source material. It is not an
RLHF dataset or a claim of human annotation experience. There are no model calls,
AWS resources, cloud deployment claims or paid services.
