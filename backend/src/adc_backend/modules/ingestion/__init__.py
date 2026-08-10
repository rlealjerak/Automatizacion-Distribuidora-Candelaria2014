"""
Ingestion (build-order step 3).

Owns: file upload handling, preserving the original uploaded file to S3
unmodified (versioned, never edited in place), and parsing both PDF
(multi-column catalog layouts, embedded images, inconsistent headers) and
Excel/CSV supplier lists into a common raw-row representation.

Not yet implemented.
"""
