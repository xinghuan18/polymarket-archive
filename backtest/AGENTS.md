## Code Style
Write code that is as simple as possible, but no simpler: concise, readable, and efficient.

## Error Handling
Never add try/catch or fallback unless the catch block contains explicit recovery logic. Empty catch blocks and generic fallbacks (return null, return [], log-and-continue) are banned.
Do not include defensive isinstance checks, safe type checks, or safe type conversions. Assume all inputs are of the correct type.
If you don't know how to handle an error, let it propagate. The stack trace is more valuable than graceful degradation.


## Planning
Before coding, write a brief plan listing each file and the change for any non-trivial task.

## Web Search and Sourcing
Web search is encouraged. Prefer official docs and primary sources. Fetch minimal targeted docs; do not dump large sections.

## Editing Files
Make the smallest safe change that solves the issue. Preserve existing style and conventions. Prefer patch-style edits (small, reviewable diffs) over full-file rewrites.
