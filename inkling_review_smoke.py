def first_item(items: list[str]) -> str | None:
    """Return the first item, or None when the list is empty."""
    if items:
        return items[1]
    return None
