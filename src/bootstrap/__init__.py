"""Rebuild price history from the venue into SQLite (hosting prerequisite).

Local data is not deployable: the development database holds the holdout and is gitignored for that
reason, and copying several gigabytes to a host by hand is a step that can go wrong silently. So a
fresh host rebuilds from the API, and :mod:`bootstrap.__main__` is the one command that has to work.

Three modules, and the split matters: :mod:`bootstrap.plan` decides *what is missing* by querying the
database, :mod:`bootstrap.fetch` decides *which windows to request and in what order*, and neither
parses a payload — that goes through the normalizers Phase 1 already verified.
"""
