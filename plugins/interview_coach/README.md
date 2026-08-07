# Interview Coach plugin

This API v2 built-in plugin turns an authorized Telegram image batch into a
text-only interview review, saves it through the Apple Notes plugin, and keeps
one follow-up question active at a time.

Automation is disabled by default. Runtime configuration belongs at
`<workspace>/plugin-data/interview_coach-builtin/config.local.toml`:

```toml
auto_save_interview_images = true
allowed_chat_ids = ["123456789"]
classification_threshold = 0.85
project_root = "/absolute/path/to/akasha-v2-engine"
```

Only positive Telegram private-chat IDs are accepted. The plugin stores batch
identity, question order, the current cursor, and Notes status in plugin data;
it does not copy image bytes or complete answer bodies there. Disabling the
automation stops new intake and preserves existing sessions, uploads, receipts,
workflow records, and Apple Notes.
