# AGENTS.md

This repository is a small Flask-based web client for Codex App Server.

## Scope
- These instructions apply to the entire repository.

## Project Structure
- `app.py` serves the Flask app, login flow, folder APIs, and `/upload`.
- `templates/index.html` contains nearly all frontend logic, including:
  - WebSocket RPC calls to Codex App Server
  - thread list rendering
  - attachment upload / paste upload
  - rate-limit display
  - message composition and send flow
- `uploads/` stores uploaded files and should be treated as runtime data.
- `thread_folders.json` is local runtime state and should not be restructured casually.

## Working Style
- Prefer minimal, surgical changes.
- Preserve the current single-file frontend architecture unless a refactor is explicitly requested.
- Do not introduce frameworks, bundlers, or build steps.
- Keep the UI behavior consistent with the existing product unless the user asks for a UX change.

## Frontend Rules
- Reuse existing functions before adding new ones.
- For attachment features, keep file selection, paste upload, preview, and send behavior aligned.
- Do not leak hidden system/control text into user-visible thread titles or message previews.
- When rendering thread titles, prefer concise, readable user-facing text.
- When showing timestamps, support both camelCase and snake_case server fields if needed.

## Backend Rules
- Keep `/upload` response shape backward-compatible.
- Do not add strict file-type restrictions unless explicitly requested.
- Avoid changing auth behavior, access code flow, or session handling unless the user asks.

## Safety / Execution
- Prefer actual sandbox and permission controls over prompt-text warnings.
- Do not append safety boilerplate into user-visible messages unless explicitly requested.

## Verification
- After frontend changes, verify the affected DOM path and data flow directly.
- When debugging thread ordering or display, inspect the real `thread/list` payload instead of assuming field names.
