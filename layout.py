"""Pure button-layout helpers (no Telegram imports, easy to test)."""

PAGE_SIZE = 30  # buttons per page (Telegram allows 100 per message)


def grid(items: list) -> list:
    """10 items or fewer -> one per row (vertical).
    More than 10 -> two per row."""
    cols = 1 if len(items) <= 10 else 2
    return [items[i:i + cols] for i in range(0, len(items), cols)]


def paginate(items: list, page: int, size: int = PAGE_SIZE):
    """Return (items_on_page, page, total_pages) with the page clamped."""
    pages = max(1, -(-len(items) // size))
    page = min(max(page, 0), pages - 1)
    return items[page * size:(page + 1) * size], page, pages
