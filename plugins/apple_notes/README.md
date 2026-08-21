# Apple Notes plugin

This API v2 built-in plugin exports authorized Markdown into a dedicated Apple
Notes folder. Authorization is normally a current explicit request; the sole
automation exception is the scoped Interview Coach flow described by decision
0036. It supports preview, create, append, status, idempotent retry, and durable
local receipts without persisting note bodies.

The plugin can execute directly in a native macOS runtime, or use the
authenticated live-only Mac Notes Bridge when Roxy runs on Linux. Remote
writes are never queued: an offline Mac returns the complete content to the
current conversation, and reconnecting does not backfill it. Its configuration belongs at
`<workspace>/plugin-data/apple_notes-builtin/config.local.toml`; the source
model documents defaults. `execution_mode` is `auto`, `local`, or `remote`;
`auto` selects the runtime-owned Bridge on non-macOS when it is enabled.
The cloud listener remains loopback-only; deployments without a WSS domain can
use the companion CLI's fail-closed launchd SSH tunnel and connect the daemon to
`ws://127.0.0.1:6330/ws`.

`folder` is external user data rather than a runtime identifier. Its historical
default remains `Akashic` so upgrading never silently creates or writes a second
Notes folder. Set `folder = "Roxy"` explicitly only when you intend to use a
new folder; the plugin never moves, renames, scans, or deletes existing notes.

The write surface is intentionally narrow: it creates notes inside one configured
folder and appends only to notes whose IDs are already mapped in the plugin's
receipt database. It does not enumerate user notes, replace bodies, or delete
content. The provenance footer is mandatory because its operation marker is the
only safe way to reconcile an uncertain external write without replaying it.
The remote path additionally uses `propose → proposal_accepted → commit → result`:
disconnect before commit is known not to have written, while disconnect after
commit is `outcome_unknown` and is never replayed automatically.
