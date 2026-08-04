"""Branch D: a fixed catalogue of source documents and a deterministic parser.

`catalogue.py` names the documents this demo can ingest; `parse.py` turns one
of them into chunks with a stable identity (`source_key`, `chunk_ordinal`) so
an evaluation gold set can name a chunk and still mean the same chunk after a
re-parse.
"""
