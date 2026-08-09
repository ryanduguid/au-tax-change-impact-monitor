# AU Tax Change Impact Monitor

A **provenance-first change-review queue**, not a tax-answering system or an automatic skill updater.

The first version compares fabricated source-index metadata with a fabricated Register-observation contract. It keeps important states distinct—`SUPERSEDED`, `CURRENT_NO_PUBLISHED_COMPILATION`, `NO_LONGER_IN_FORCE`, and `LOOKUP_FAILED`—then maps only exact register ID + collection pairs to a potential workflow-review question.

```text
Synthetic source index + synthetic Register observation + exact source-to-skill map
                                      |
                                      v
                          Change classification and scope gate
                                      |
                                      v
                         Potential-impact technical-review queue
                                      |
                                      v
                         Human technical-tax decision, outside this tool
```

## Demo

```bash
python -m pip install -e ".[dev]"

au-tax-change-impact-monitor compare \
  --baseline au_tax_change_impact_monitor/samples/baseline/sample-sources.json \
  --observation au_tax_change_impact_monitor/samples/observations/sample-register-observation.json \
  --map au_tax_change_impact_monitor/samples/mappings/sample-source-skill-map.json \
  --out build/demo
```

The sample fixtures ship inside the package, so a plain `pip install` can run the same demo from any directory; `python -c "from au_tax_change_impact_monitor.util import sample_path; print(sample_path())"` prints their installed location. Every input option accepts any readable path, and `--out` is created relative to the current directory.

The example creates one `SUPERSEDED` source item mapped to a BAS-review question. It deliberately does not infer the legal effect of the change, update a skill, or send a notification.

The output directory contains deterministic `impact-queue.json` and `impact-queue.md` files. An item is `OPEN` when it needs human technical review; it is `BLOCKED` when scope is incomplete, a lookup failed, or a current version has no published compilation.

```bash
au-tax-change-impact-monitor validate-review \
  --queue build/demo/impact-queue.json \
  --decision path/to/a-human-technical-review.json
```

Only `AWAIT_PRIMARY_TEXT`, `NO_WORKFLOW_CHANGE`, `UPDATE_CANDIDATE`, and `ESCALATE_TECHNICAL_REVIEW` are accepted. Validation checks the decision structure and matching queue only; it does not certify the review, edit a skill, or establish a legal conclusion.

## Strict scope

- Inputs are metadata only. No legislation EPUB, HTML, PDF, section JSONL, rate, or source text is read or stored.
- The synthetic demo never performs network I/O or Register scraping.
- A source is mapped by exact `(register_id, collection)` only. An unmapped change remains visible as `UNMAPPED_SOURCE`; it is never silently dismissed.
- `UNCHANGED` is valid only inside a complete observation scope. A partial/failed observation cannot produce a “no change” conclusion.
- Every artefact carries `mode: synthetic` to prevent the demo being misrepresented as a live legislative monitor.

## Relationship to existing work

The intended future source is a reviewed, deliberately versioned observation output from [au-tax-legislation-corpus](https://github.com/ryanduguid/au-tax-legislation-corpus). That corpus’s distinction between a superseded compilation, a current version with no published compilation, a title no longer in force, and a failed lookup is preserved here. This project is not a replacement corpus builder and must not treat derived corpus material as authorised legal text.

## Development

```bash
pytest
python -m build
```

Built with AI assistance (Claude); design, review, and testing by the author.

MIT licensed.
