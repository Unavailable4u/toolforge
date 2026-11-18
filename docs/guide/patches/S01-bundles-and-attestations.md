# S01: Optional: portable bundles and signed provenance

Depends on: P12. Not required for the release. If built, P21 checks it and the site may list it as `built`.

## Goal

Let one operator hand a verified tool to another, with tamper-evident provenance, **without the receiver having to trust it**.
The receiver learns who made the bundle and that its history is intact. The receiver's own pipeline still decides whether the
tool is safe. A signature proves origin, never safety.

## Scope

`src/toolforge/provenance/{keys.py,attest.py,bundle.py}`, CLI `toolforge keys init|show|trust|untrust|list`,
`toolforge export <name>@<version> -o file.tfbundle`, `toolforge import file.tfbundle`, `toolforge verify-bundle file.tfbundle`,
tests, `docs/PROVENANCE.md`. Adds one dependency (`cryptography`, pinned) for Ed25519; the standard library cannot sign.

## Bundle format

A `.tfbundle` is a deterministic tar containing:

```
bundle.json        schema version, tool name, version, created_at, toolforge version
manifest.json      the manifest
package.tar.gz     the frozen package blob
ledger.jsonl       the verification ledger (hash chained)
report.json        the Blast Radius report
attestation.json   signed statement (below)
```

## Attestation

```json
{"schema": 1, "name": "csv_summary", "version": "1.0.0", "code_sha": "...", "blob_sha": "...",
 "gates": [{"id": "G3", "status": "passed", "summary_sha": "..."}],
 "ledger_head": "<hash of the last ledger event>",
 "toolforge_version": "0.1.0", "runtime_image_digest": "...", "minilocker_commit": "...",
 "models": {"challenger": "<id>" | null, "builder": "<id>" | null},
 "created_at": "...", "signer": "<public key fingerprint>", "signature": "<base64 Ed25519 over the canonical JSON of the rest>"}
```

Canonical JSON: sorted keys, no whitespace, UTF-8. Keys live in `$TOOLFORGE_HOME/keys/` with file mode 0600; the private key is
never printed, exported, logged or put in a ledger. `toolforge keys show` prints only the public fingerprint.

## Import rules (the point of this patch)

1. Verify the tar is well formed and within size caps; reject unknown or oversized members and unsafe paths.
2. Recompute `blob_sha` and `code_sha` from `package.tar.gz`; verify the ledger hash chain; check `ledger_head`.
3. Check the signature against the **trusted key list** (`toolforge keys trust <public-key-file>`). An unknown signer is allowed
   but displayed as "unsigned by anyone you trust".
4. Register the version as `submitted` with `created_by = "import:<fingerprint>"`, then **run the full local pipeline**. Imported gate
   results are shown as informational provenance and are never copied into the local `gate_results`, never shortcut a gate and never
   change the status. Network and secret permissions follow the normal approval flow.
5. Name collision and version immutability rules apply exactly as for a submission.

`toolforge verify-bundle` performs steps 1 to 3 only and prints a clear summary; it changes nothing.

## Tests

Round trip export, verify, import into a fresh home; tampered blob, edited ledger line, wrong `ledger_head`, swapped signer and
a flipped signature byte each fail with a specific message; unknown signer shown as untrusted but importable; imported tool
still runs every local gate (assert with `FakeRunner` that gates executed); private key file mode and absence from all output;
canonical JSON stability; a bundle with a path-traversal member is rejected.

## Acceptance

`pytest -q`; `toolforge keys init`, `toolforge export csv_summary@1.0.0 -o x.tfbundle`, `toolforge verify-bundle x.tfbundle`.

## Pitfalls

Do not let a valid signature raise a tool's status or skip any gate. Do not add a "trust everything from this signer" auto-promote.
Do not store or transmit the private key anywhere. Describe in `docs/PROVENANCE.md` what a signature does and does not mean.
