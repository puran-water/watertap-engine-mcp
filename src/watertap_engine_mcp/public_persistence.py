"""Explicit boundary for integrations not distributed with the public engines."""

def persist_named_artifact(*args, **kwargs):
    raise NotImplementedError("Database publication is not included in this public package.")

persist_engineering_artifact = persist_named_artifact
persist_stream_snapshot = persist_named_artifact
