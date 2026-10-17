# todo-api

## Decisions

- Storage is SQLite through the standard library; no ORM.
- Pagination is cursor-based (`?after=<id>`), not offset-based.
