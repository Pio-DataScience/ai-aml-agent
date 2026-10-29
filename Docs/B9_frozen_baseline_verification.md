# B9 frozen baseline hash verification

The frozen candidate artifact `B9_frozen_baseline_20261029.json` was read and verified without modification.

- Raw file bytes: SHA-256 `8243cc92268c78560f27b09adf8031918eb0b7ad962f1797d589eb61735cc7ad`.
- Canonical manifest digest: parse as UTF-8 JSON; remove only the root `baseline_sha256` property; serialize the remaining object with Python `json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")`; SHA-256 the resulting bytes. Result: `9772b9b5f632114356b2d64877b3ba757d3b81f20d0510f76f945bfcf5ff0ca6`.
- The canonical digest matches both the embedded `baseline_sha256` value and the previously verified original value. The different raw-file digest is expected because it covers the full pretty-printed file, including the embedded digest.

The previously reported `d287cd...` canonical value was a reporting error. The baseline file has not been rewritten, and this correction does not indicate a change to any SQL candidate. The companion immutable live snapshot is `B9_live_snapshot_bd9bf5e7_20261029.json`; its digest covers canonical sorted compact UTF-8 JSON excluding only its root `snapshot_sha256` field.
