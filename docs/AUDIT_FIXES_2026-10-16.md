# Audit fixes — 2026-10-16

The seven findings from the repository audit are fixed on `main`. No new runtime
dependencies, database migrations or changes to tool integrity verification were
needed.

| Finding | Change | Regression check |
| --- | --- | --- |
| Concurrent cover work loses assets | Append against the latest media record in one SQLite transaction. Conversion publication merges the latest assets in its own transaction and emits the committed record. Failed attachment removes the new file. | Hold a fake AI response, export another cover, update publication metadata, then release AI; both assets and the newer metadata survive. A separate test publishes from a stale conversion record. |
| Remote login accepts private destinations | Use the same public-address rule for literal URLs and resolved SOCKS5 destinations. Connect to the checked IPs; Chromium sends redirects, subresources and WebSocket TCP traffic through a session-owned loopback proxy with no direct fallback. | Reject loopback, private, mapped IPv4, link-local and special-use addresses; send real SOCKS5 requests for localhost and mapped loopback and confirm that a local listener receives nothing. |
| Processing folder disappears after save | Keep the chosen folder in desktop settings responses. Remove it at the web response/event boundary. | Save, reopen storage and parse the returned settings; the folder survives. Web tests retain path rejection and strip the field from responses. |
| Deleted task secrets remain | Delete each task's cookie/proxy snapshots even when retaining its media files. Release the cookie snapshot after source freezing has durably saved its manifest. | Delete tasks with both `deleteFiles` values; snapshots disappear and the unrelated AI key survives. |
| Custom processing workspace remains | Creation and deletion share the workspace calculation. Delete only the task's named directory after checking its canonical parent and rejecting links. | Check custom/default workspace deletion, retained files, and an unrelated sentinel file; reject a directory with the wrong name. Unix tests also reject a linked workspace. |
| Other clients retain deleted media | Emit `media.remove` when deleting media and remove its row, task mapping and selection in the React reducer. | Check backend events and another client's state. Removing a task while keeping its files still retains media. |
| Web tool installation always returns 403 | Show tool installation only on desktop. Explain Docker image updates in the web interface. | Render settings for both hosts; web has no install button, desktop retains it. |

Validation used Node.js **24.16.0** and the installed Rust toolchain:

- `cargo test --locked --release --workspace -- --include-ignored --test-threads=1 --skip performance_audit_baseline`: **40 passed**, including all four real media acceptance tests.
- `npm test --prefix frontend`: **19 passed**, six opt-in performance cases skipped.
- `npm run build --prefix frontend`: TypeScript and production build passed.
- `node --test tests/*.test.mjs`: **8 passed**; verified tool acquisition self-check passed.
- `cargo clippy --locked --workspace --all-targets -- -D warnings`, formatting and diff checks passed.
- A separate isolated run used the installed headless Microsoft Edge with the
  production browser/proxy source. Public navigation, screencast frames,
  mouse/text input, fake-cookie save and profile cleanup passed. Later CDP
  navigation to `127.0.0.1`, `localhost` and mapped IPv4 was blocked, and the local
  fixture received zero connections. This harness stayed outside tracked source.

The container browser check now installs its controlled login page through CDP
after public navigation; it no longer relies on a private login destination.
Its CDP relay uses `docker exec`, leaving the debug port unpublished.

Docker's Linux engine was unavailable locally, so Linux/amd64 and arm64 container
execution remains for CI. Local DNS maps public hostnames into `198.18.0.0/15`;
these addresses stay blocked, and the live browser check used a public IP URL.
Deployments using fake-IP DNS need public address resolution for remote login.
Previously orphaned files from tasks already deleted were not swept from user
storage. The fixes govern future operations and retained tasks that are deleted.

Network behavior follows [Chromium's proxy documentation](https://chromium.googlesource.com/chromium/src/+/HEAD/net/docs/proxy.md).
Address exclusions were checked against the IANA
[IPv4](https://www.iana.org/assignments/iana-ipv4-special-registry/)
and [IPv6](https://www.iana.org/assignments/iana-ipv6-special-registry/) registries.
