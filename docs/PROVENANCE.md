# Portable bundles and signed provenance (optional patch S01)

One operator can hand a verified tool version to another as a single file. The receiver learns **who made the file** and **that its history
is intact**. The receiver's own pipeline still decides whether the tool is safe: a signature proves origin, never safety.

```
toolforge keys init                              # once: create your signing key
toolforge export csv_summary@1.0.0 -o csv_summary.tfbundle
toolforge verify-bundle csv_summary.tfbundle     # on the other machine: checks only, changes nothing
toolforge keys trust their-signing.pub --label alice   # optional: say whom you know
toolforge import csv_summary.tfbundle            # register it and run every local gate
toolforge promote csv_summary@1.0.0 --reason "..."     # still yours to do, as for any submission
```

These are operator commands. No MCP tool reaches them (a test reads the source of the server package to prove it).

## What a signature means, and what it does not

A valid signature means:

* the person who holds the signing key wrote down the statement in `attestation.json`, and nobody has changed it since;
* the statement names the hashes of the package and of the verification ledger, and the files in the bundle match those hashes;
* the ledger is a consistent hash chain that is about this tool version with this code, and the gate outcomes in the statement are the ones
  the ledger records.

A valid signature does **not** mean:

* **That the tool is safe, or correct.** Anyone can generate a key and sign a bundle of anything that is internally consistent. That is why a
  signature from a key you did not choose to trust is still shown as valid, and is described as "unsigned by anyone you trust".
* **That the gates ran honestly.** The ledger is a hash chain, not a witness: it shows the events were not edited afterwards, but a signer who
  controls their own machine could have written a ledger that never came from a real run. For this reason what a bundle says about gates is
  displayed as information and is never used for anything.
* **That the date, the models, the runtime image or the ToolForge version are true.** The signer states them; nothing checks them.
* **That the signer is still who you think.** There is no key expiry and no revocation list. If a key is lost or stolen, each receiver has to
  `toolforge keys untrust` it.

Trust is a label on a signer and nothing else. **It never changes a version's status, never skips a gate and never promotes anything.** There is
no "trust everything from this signer" setting.

## What `import` does

1. The archive is read member by member and nothing is extracted. See the table of limits below.
2. `blob_sha` and `code_sha` are recomputed from the bytes in the bundle. The ledger is parsed, its hash chain is verified with MiniLocker's own
   `verify_events`, `ledger_head` must be the hash of its last event, the ledger must be about this very version and code, and the attested
   gate outcomes must be the ones the ledger records.
3. The signature is checked, and the signer is looked up in your key list: your own key, a key you trust, or nobody you chose.
4. The package is submitted exactly as `toolforge submit` would, with `created_by = "import:<fingerprint>"`. It is registered as `submitted` and
   the whole local pipeline runs. Nothing the bundle says is copied into your registry's `gate_results`. A tool that asks for network access or
   secrets waits in `pending_approval` like any other. The tool's own gates build the frozen package again on your machine, so on a tool with
   `requirements` the local blob is not necessarily byte-identical to the one in the bundle.
5. Name collisions and version immutability are the registry's own rules: a bundle cannot replace a version that exists with other content
   (`version_exists`), and cannot take the name of an active tool unless it names that version as its parent (`name_collision`).

`toolforge verify-bundle` performs steps 1 to 3 and changes nothing. It needs MiniLocker (the git submodule) to verify the chain, but not Docker.
`import` needs what `submit` needs, including Docker for gates G2 to G4.

## The bundle

A `.tfbundle` is a deterministic, uncompressed tar of exactly six plain files. Exporting the same version with the same clock gives the same bytes.

| File | Holds | Limit |
| --- | --- | --- |
| `bundle.json` | schema, tool name, version, `created_at`, ToolForge version | 16 KB |
| `manifest.json` | the manifest | 1 MB |
| `package.tar.gz` | the frozen package, byte for byte as the registry stores it (its SHA-256 is `blob_sha`) | the runner's unpack limit |
| `ledger.jsonl` | the verification ledger of the version (MiniLocker's hash chain) | 8 MB, 100 000 events |
| `report.json` | the Blast Radius report (the stored one, or one built from the exported ledger) | 1 MB |
| `attestation.json` | the signed statement | 64 KB |

The whole bundle is at most 64 MB. Only the version's newest verification run that holds gate events is exported, not the calls made since.
A version can be exported when it is `verified_basic`, `verified`, `pending_approval`, `active` or `deprecated`; a rejected, still-verifying or
quarantined version cannot. Export verifies the bundle it just built with the same code an importer runs, before a byte is written.

### The attestation

```json
{"schema": 1, "name": "csv_summary", "version": "1.0.0", "code_sha": "...", "blob_sha": "...",
 "gates": [{"id": "G3", "status": "passed", "summary_sha": "..."}], "ledger_head": "...",
 "toolforge_version": "0.0.1", "runtime_image_digest": "sha256:..." | null, "minilocker_commit": "...",
 "models": {"challenger": "<id>" | null, "builder": "<id>" | null}, "created_at": "...",
 "signer": "<fingerprint>", "signer_key": "<base64 public key>", "signature": "<base64 Ed25519>"}
```

* The signature is Ed25519 over the canonical JSON (sorted keys, no whitespace, UTF-8) of every field except `signature`.
* `attestation.json` must itself be that canonical JSON, byte for byte. A re-spaced file, a repeated key or `true` where `1` belongs is refused,
  so the file cannot read differently in another JSON program than it did here.
* `signer_key` carries the signer's public key so a stranger's signature can be checked for integrity. Whether to trust the signer is decided only by
  your own key list. The fingerprint is the first 128 bits of the SHA-256 of the raw public key, 32 lower-case hex characters, and it is always
  computed from the key's bytes, never taken from a file name or a label.
* `models.challenger` comes from the G5 event in the ledger; `models.builder` from the build ledger, for a version the builder made. Either is null when
  not recorded. Only ids made of printable ASCII without spaces are accepted.
* Every field is required, unknown fields are refused, and an error names the field and the kind of mistake, never the value found there.

## Keys

```
$TOOLFORGE_HOME/keys/signing.key        your private key: one line, mode 0600, in a 0700 folder
$TOOLFORGE_HOME/keys/signing.pub        the matching public key: one line, safe to give to anyone
$TOOLFORGE_HOME/keys/trusted/<fp>.pub   public keys you chose to trust
```

* The private key is never printed, exported, logged or put in a ledger, and no message or file other than `signing.key` holds its text.
  It is read only by `export`, and only when no group or other permission bit is set (on a filesystem that has permissions).
* `keys init` refuses to run when a key exists, because replacing it would orphan every bundle it signed. **ToolForge cannot recreate a lost key; back
  up `signing.key` yourself.**
* On WSL2 keep `$TOOLFORGE_HOME` on the Linux filesystem: under `/mnt/c` the 0600 mode cannot be kept and export refuses to use the key.
* `keys trust` refuses a private key file with a warning and copies nothing. Your own key cannot be removed with `keys untrust`.
* The trusted list holds at most 500 keys.

## Errors

Every refusal has its own code, shown as `error [code]: message` with a `hint:`. A `do not import this bundle` hint means the file was changed
after it was made.

| Code | Means |
| --- | --- |
| `attestation_invalid` | `attestation.json` is not valid JSON, is too large, or a field is missing, unknown or the wrong shape |
| `attestation_not_canonical` | the attestation file is not exactly the canonical JSON of what was verified |
| `bad_fingerprint` | a fingerprint is 32 lower-case hex characters |
| `bad_option` | an option is out of range (`--fuzz-inputs`) |
| `blob_sha_mismatch` | the frozen package is not the one the attestation names |
| `blob_unusable` | the frozen package is not an archive the runner accepts (links, unsafe names, not text, too large) |
| `bundle_inconsistent` | `bundle.json`, `manifest.json` and the attestation name different tools or versions |
| `bundle_invalid` | `bundle.json`, `manifest.json` or `report.json` is not what it should be |
| `bundle_member_duplicate` | a member appears twice |
| `bundle_member_missing` | one of the six files is absent |
| `bundle_member_too_large` | a member is over its limit |
| `bundle_member_unknown` | a member is not part of the format |
| `bundle_member_unsafe` | an unsafe path, or a member that is a link, folder or device |
| `bundle_missing` | the bundle file does not exist |
| `bundle_too_large` | the bundle is over 64 MB |
| `bundle_unreadable` | not a readable, uncompressed tar, or not a regular file |
| `code_sha_mismatch` | the code in the package does not hash to the attested `code_sha` |
| `gates_mismatch` | the attested gate outcomes are not the ones the ledger records |
| `key_exists` | `keys init` found a key already |
| `key_invalid` | a key file is damaged, too large or not text |
| `key_is_private` | a private key was given where a public key is needed |
| `key_missing` | a key file does not exist |
| `key_permissions` | the private key can be read by other users |
| `key_unreadable` | a key file cannot be read (or is a link) |
| `ledger_chain_broken` | the ledger's hash chain does not verify |
| `ledger_head_mismatch` | the last ledger event is not the signed `ledger_head` |
| `ledger_invalid` | a ledger line is not a JSON object, or a local ledger cannot be read cleanly |
| `ledger_mismatch` | the ledger is about another tool, version or code |
| `manifest_mismatch` | the manifest inside the package is not the bundle's manifest |
| `no_blob` | the version was never frozen |
| `no_ledger` | there is no verification ledger on this machine to export |
| `no_signing_key` | run `toolforge keys init` first |
| `not_canonical` | a value cannot be written as canonical JSON |
| `not_exportable` | the version has not passed verification, or is quarantined |
| `output_exists` | the output file exists; choose another or add `--force` |
| `output_unwritable` | the output folder cannot be written |
| `package_invalid` | the package in the bundle cannot be rebuilt into an archive |
| `report_unavailable` | the Blast Radius report cannot be built or is too large |
| `signature_invalid` | the signature does not match the statement |
| `signer_mismatch` | the signer fingerprint is not that of the key in the attestation |
| `too_many_keys` | the trusted list is full |
| `unknown_version` | no such version in the registry |

## Not measured

* Exporting and importing a tool that was verified through real Docker containers (the tests use scripted gates and the stand-in sandbox, and
  the real MiniLocker ledger).
* Behaviour on Windows without WSL2: file modes cannot be enforced there, so the private key's permission check is skipped.
