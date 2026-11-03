# SCADA Engineering Data Analyzer

**Release status: BOQ PDF Row Parsing Integration (`v1.6.0`)**

| Tag | Status |
| --- | --- |
| `v1.5-semantic-validation-candidate` | Semantic Validation Candidate — 151/151 tests PASS · Real Project Round 1: FAIL |
| `v1.6.0` | BOQ PDF Row Parsing Integration — 219/219 tests PASS · Synthetic BOQ regression PASS · Non-BOQ PDF negative validation PASS · Real BOQ validation NOT TESTED · Production Approval for BOQ parser NOT APPROVED |
| `v1.5.2` | Evidence Metadata Quality — 203/203 tests PASS · Real Project Round 3: PASS (17/17) · Production Approval: NOT APPROVED |
| `v1.5.1` | Semantic Bugfix Baseline — 171/171 tests PASS · Real Project Round 2: PASS (P0 regression scope) · Production Approval: NOT APPROVED |

`v1.5.1` fixes the three P0 defects found in real-project Round 1 (MTEXT `\U+XXXX` decoding, unavailable-source cross-reference semantics, PDF clause-level citation). Summary: [docs/REAL_PROJECT_VALIDATION_STATUS.md](docs/REAL_PROJECT_VALIDATION_STATUS.md).

This version is not Production and is not an AI Assistant.

| Validation area | Status |
| --- | --- |
| Functional Regression | PASS — 219/219 tests (151 from v1.5 + 20 Round 1 + 32 P1 + 16 BOQ synthetic tests) |
| Source Integrity | PASS — 13 test/synthetic sources, 0 hash changes |
| Parser Integrity | PASS — verified test/synthetic scope |
| Normalized Data Integrity | PASS — verified CSV/SQLite consistency |
| Engineering Semantics | PASS — defined test scope only |
| Acceptance Gate | PASS — 9/9 |
| Real Project Validation | SB12 Round 1 FAIL (v1.5) → Round 2 PASS for the P0 fixes (v1.5.1); Round 3 PASS for the P1 evidence metadata (v1.5.2); SELF-REVIEWED; IFC / BOQ / Clash not tested |
| Human Engineering Approval | NOT APPROVED |
| Production Approval | NOT APPROVED |

Evidence: [semantic validation report](SEMANTICS_REPORT.md), [test results](semantics_regression_results.txt), [acceptance evidence](regression_semantics/evidence.json), [HTML review](regression_semantics/index.html).

## Feature freeze status: PARTIALLY LIFTED

Allowed (controlled, bounded, reversible):

- Evidence-first BOQ row parser integration (PDF tables -> existing `boq_items` / evidence chunks)
- Cable Tray Designer bridge feasibility testing (no formal integration)
- Regression tests / validation tooling
- Bug fixes and evidence-quality improvements
- DWG batch-tool upgrade (2026-10, user-approved): DWG revision diff, title-block attribute batch read/write, layer standard check report, parallel accoreconsole execution (`workers`), test coverage work

Still frozen:

- Retrieval / RAG
- Ollama
- Dify
- Open WebUI integration
- New unrelated parsers (other than the DWG batch-tool features listed above)
- Production automation

Previously developed retrieval modules remain unintegrated. Test success does not authorize lifting the remaining freeze or changing approval status.

## BOQ PDF row parser (v1.6.0 candidate)

| Item | Status |
| --- | --- |
| Implementation | PASS |
| Synthetic regression | PASS (gates B1–B10, `tests/test_pdf_boq.py`) |
| Real BOQ validation | NOT TESTED — no real project BOQ available |
| Production Approval for BOQ parser | NOT APPROVED |

Only the row-parsing logic was ported (page + table + row location, table-scoped headers, continued-table rule, raw quantity text kept with a warning). No second schema: rows go to `boq_items`; evidence chunks are built by the existing evidence builder. Modes: `pdf_boq_tables` = `auto` (default) / `required` / `off`. Continued-table detection is a known heuristic (next page, first table, same column count), not proven on real BOQs. Optional dependency: `pdfplumber` (`requirements-pdf-tables.txt`, not in the main requirements); without it the PDF text evidence is still produced and the BOQ step is recorded as `SKIPPED_DEPENDENCY`. The one real PDF available (a requirements appendix, not a BOQ) produced 0 BOQ rows, as expected.

## Next gate: real project validation

Await user-designated company project paths and an engineering reviewer. No real company project data or human engineering approval has been established by the existing fixture runs.

Minimum source set from one relevant project:

- One real DWG/DXF drawing set.
- One IFC model.
- One actual BOQ.
- One specification PDF.
- One SoW/DOCX.
- Clash XML, if available.

Keep sources read-only and write regression results into a separate output directory. Record source hashes before/after, confirmed revisions (unknown remains unknown), source inventory and per-file failures. Freeze the output being reviewed; corrections generate a new review run rather than replacing reviewed evidence.

For each sampled case, trace SOURCE → PARSED → NORMALIZED → CROSS REFERENCE → RESULT. Retain the exact file and location: CAD handle/layer/coordinates; IFC GlobalId; Excel sheet/row/item; PDF page/section; DOCX table/row or paragraph; clash ID/object identifiers.

Required engineering spot-checks include:

- RTU01 across CAD, IFC and BOQ: verify real presence, name normalization and that no different RTU was associated.
- UPS supply requirements: verify a related object can be found without automatically setting compliance_status to SUPPORTED.
- IFC Space/System/Building Storey: verify these do not enter the equipment missing list.
- Verify NULL, explicit empty values, revision and hash semantics against the source.

Record reviewer findings, including correct matches, false positives, false negatives and uncertain cases. Each record must retain its source locations, observed result, engineer's expected interpretation, review disposition and reviewer/date. False-negative review must sample source items independently of the tool's returned matches. Define denominators and acceptable error thresholds with the engineering reviewer; no threshold is assumed by this candidate release.

## Conditions to resume retrieval

Resume only after real project validation establishes all of the following and the user authorizes continuing:

- Cross-reference error rates meet the reviewer-agreed acceptance criteria.
- Evidence locations are correct.
- NULL, revision and hash semantics are correct.
- Human spot-checks reveal no unresolved structural errors.

Approved continuation order: Evidence Retrieval → FTS5 → Structured Query → Ollama → Dify. Keep SQL/evidence-based results authoritative, with LLM processing last. Resuming development does not itself grant Production Approval.
