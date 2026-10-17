# todo-api notes

## 2026-01-12

Cursor pagination returned duplicate rows when two todos shared a created_at timestamp.

## 2026-01-14

Fixed: the cursor now orders by (created_at, id). test_pagination_no_duplicates passes locally.
Not yet deployed to staging.
