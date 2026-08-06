---
name: apple-notes-export
description: Use when the user explicitly asks Akashic to save important content to Apple Notes or append to a note previously created through this plugin.
---

# Apple Notes Export

Use this skill only when the current user message explicitly asks to save, record,
export, or append content in Apple Notes. Do not infer consent from importance,
memory extraction, consolidation, proactive runs, scheduled jobs, or an earlier turn.

## Workflow

1. Prepare a concise title and polished Markdown. Preserve facts; do not invent
   missing content.
2. Call `apple_notes_preview` when formatting is uncertain or the content is long.
3. For a new note, call `apple_notes_create` with a stable `document_key` using
   only letters, numbers, `.`, `_`, `:`, and `-`.
4. To add a section, call `apple_notes_append` only with a `document_key` that
   was created by this plugin.
5. Report the returned status accurately. `committed` means Notes returned a
   concrete note receipt; `outcome_unknown` requires status/reconciliation and
   must never be described as success.

Prefer `flow_chain` for system flows and architecture, `knowledge_card` for
general learning notes, and `plain` when the user requests minimal formatting.
Never delete, replace, scan, or edit arbitrary Notes. Never expose internal note
IDs, receipt paths, source references, or session hashes unless the user asks for
diagnostics.
