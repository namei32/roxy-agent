# Apple Notes plugin

This API v2 built-in plugin exports authorized Markdown into a dedicated Apple
Notes folder. Authorization is normally a current explicit request; the sole
automation exception is the scoped Interview Coach flow described by decision
0036. It supports preview, create, append, status, idempotent retry, and durable
local receipts without persisting note bodies.

The plugin requires a native macOS runtime and Apple Events permission for the
runtime process to control Notes. Its configuration belongs at
`<workspace>/plugin-data/apple_notes-builtin/config.local.toml`; the source
`config.local.toml` documents defaults only.

The write surface is intentionally narrow: it creates notes inside one configured
folder and appends only to notes whose IDs are already mapped in the plugin's
receipt database. It does not enumerate user notes, replace bodies, or delete
content. The provenance footer is mandatory because its operation marker is the
only safe way to reconcile an uncertain external write without replaying it.
