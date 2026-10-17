"""A pretend project for the quickstart. The brain never reads code it is not pointed at."""


def list_todos(rows, after=None, limit=20):
    rows = sorted(rows, key=lambda r: (r['created_at'], r['id']))
    if after is not None:
        rows = [r for r in rows if r['id'] > after]
    return rows[:limit]
