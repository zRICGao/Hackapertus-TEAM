"""Fail-closed placeholder for the official export boundary."""


def export_findings(records: list[dict]) -> None:
    if not records or len(records) > 5:
        raise ValueError("Expected 1–5 confirmed findings")
    if any(item.get("mode") != "live" or item.get("status") != "confirmed" for item in records):
        raise ValueError("Only independently confirmed live findings may be exported")
    raise NotImplementedError("Official schema resolution and confirmation registry are pending")
