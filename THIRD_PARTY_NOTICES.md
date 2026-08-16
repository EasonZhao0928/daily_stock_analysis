# Third-Party Notices

This repository includes software derived from third-party projects. The
following notice applies to the files identified below.

## AlphaSift

- Project: AlphaSift
- Source: https://github.com/ZhuLinsen/alphasift
- Referenced revision: `9f522747caafd3c0b1ddb7e14d5cf44c8580b6cf`
- License: Apache License 2.0
- Included and modified files: `src/services/screening/**/*.py` and
  `src/services/screening/strategies/*.yaml`
- License copy: `src/services/screening/LICENSE`

The included code has been modified and integrated into
`daily_stock_analysis`. Per-file headers identify the source revision and
modification status.

## a-stock-data (reference only)

- Project: a-stock-data
- Source: https://github.com/simonlin1212/a-stock-data
- Referenced revision: `3a3149dedbe30cda58b5c94387039d7e707cedcd`
- License: Apache License 2.0 (reference checkout: `LICENSE`)
- Use in DSA: endpoint knowledge, field aliases, failure cases and redacted
  fixtures. The DSA adapters under `data_provider/` are independent rewrites;
  no executable code or long provider payload from the reference is copied.
- Modification statement: all DSA files carrying this reference header are
  newly authored for DSA's DataEnvelope, supplier runtime and Evidence store.
