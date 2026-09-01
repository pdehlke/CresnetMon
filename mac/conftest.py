"""Puts the repo root on sys.path so tests can import the root-level
`cresnet_replay` tool alongside the `cresnetmon` package. Empty otherwise:
pytest inserts a conftest's own directory under the default import mode.
"""
