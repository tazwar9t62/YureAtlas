"""Experiment selection, applied only after selecting an authoritative revision."""

JMA_MAGNITUDE_TYPES = frozenset({"J", "D", "d", "V", "v"})


def field(record, name, default=None):
    return record.get(name, default) if isinstance(record, dict) else getattr(record, name, default)


def eligible_cell(record, grid, config, *, minimum=None):
    """Return the retained cell, or None for a valid but non-target revision.

    Legacy revisions were admitted only as earthquakes; absent new metadata has
    that same meaning. Provider review status remains distinct from event type.
    """
    if (
        field(record, "status") == "deleted"
        or field(record, "event_type", "earthquake") != "earthquake"
        or field(record, "selection_reason") is not None
        or (config["provider"] == "jma" and field(record, "magnitude_type") not in JMA_MAGNITUDE_TYPES)
        or field(record, "magnitude") < (config["input_magnitude"] if minimum is None else minimum)
    ):
        return None
    return grid.assign(field(record, "longitude"), field(record, "latitude"))
