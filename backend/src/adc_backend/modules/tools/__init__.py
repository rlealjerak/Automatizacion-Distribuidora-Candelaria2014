"""
OpenClaw-facing tool interface (build-order step 10).

This is THE boundary OpenClaw calls into. OpenClaw owns Telegram
conversation, file uploads, and formatting - it must never contain
business logic. Everything exposed here should be a thin, well-documented
call into the modules above (trigger a run, check status, relay an
approval) - if a function here starts doing matching or calculation work
itself instead of delegating to `matching`/`rules`, that's a boundary
violation and should be flagged, not built.

Not yet implemented.
"""
