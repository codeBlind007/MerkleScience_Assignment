# NOTES.md

## Live Application

Live URL: ______________________________

Deployment / usage notes:
- URL will be added once the final deployment is available.
- No special setup or credentials are required for the assignment beyond the normal API usage described by the project.

## What I Completed

I completed all of the provided TODOs and implemented all service-layer functions that were initially marked as `NotImplemented`.

### 1. ISBN-13 validation and normalization

Implemented ISBN-13 validation in `normalize_isbn`.

Approach:
- Remove spaces and hyphens before validation.
- Verify that the normalized value contains exactly 13 digits.
- Calculate the weighted checksum using weights `1, 3, 1, 3, ...` over the first 12 digits.
- Verify that the calculated check digit matches the 13th digit.
- Return the normalized 13-digit ISBN.

This keeps ISBN storage consistent while allowing common formatted ISBN input.

### 2. Duplicate ISBN handling

Added duplicate ISBN validation in `services/books.py`.

Before creating a book, the service checks whether another book already uses the ISBN and returns `409 Conflict` when it does. The database also keeps ISBN unique so the constraint is enforced at the persistence layer as well.

### 3. Book listing and search

Implemented the book listing requirements, including:
- Case-insensitive substring search.
- Searching both title and author.
- Restricted-book filtering.
- Inclusive minimum and maximum price filters.
- Supported title/price sorting.
- Stable `id` tie-breaking.
- Pagination with `limit` and `offset`.
- Total count calculated before pagination.

#### Bug Fix: Search initially checked only the title

Initially the `q` filter only searched `Book.title`, even though the specification required matching both title and author.

Initial approach:

```python
if q:
    query = query.where(
        Book.title.icontains(q, autoescape=True)
    )
```

Fix:

```python
if q:
    query = query.where(
        or_(
            Book.title.icontains(q, autoescape=True),
            Book.author.icontains(q, autoescape=True)
        )
    )
```

The important lesson was to translate the specification literally: a search across multiple fields requires an `OR` condition rather than applying the filter to only one field.

### 4. Partial book updates

Implemented `update_book` with PATCH semantics.

Only fields explicitly supplied by the client are updated. An empty update is allowed, while explicitly supplying `null` for an updateable field is rejected according to the assignment requirements.

The service retrieves the existing SQLAlchemy model, modifies only the supplied fields, commits the transaction, refreshes the object, and returns it.

### 5. Member creation and email normalization

Implemented duplicate-email handling in `services/members.py`.

Email input is normalized before validation and storage:
- Leading/trailing whitespace is stripped.
- Email is converted to lowercase.
- The normalized value is then validated.

This also makes duplicate-email checks consistent for differently formatted versions of the same email address.

### 6. Member statistics and loan model

Implemented `get_member_stats`.

The statistics distinguish between:
- Paid orders only for `orders_paid` and `total_spent_cents`.
- All unreturned loans for active loans.
- Unreturned loans whose due date has passed for overdue loans.
- Late fees from returned loans.

I used aggregate queries rather than loading unnecessary records into Python.

The loan model was extended with:
- `due_at`
- `returned_at`
- `late_fee_cents`

Loan state is derived from these fields and the current time rather than relying on a stored status column.

### 7. Order creation

Implemented `create_order` with the required validation order:
1. Member existence.
2. All referenced books exist.
3. Restricted-book permissions.
4. Stock availability.
5. Stock reservation.
6. Price snapshotting.
7. Discount calculation.
8. Pending order creation.

The order stores each item's price at purchase time through `unit_price_cents`, so later changes to a book's price do not change historical orders.

Discounts are calculated from the total quantity of items, not the number of distinct books.

The operation uses a database transaction so stock changes and order creation are atomic. If the operation fails, the transaction is rolled back so an order cannot leave partially updated stock.

### 8. Loan lifecycle

Implemented:
- `loan_status`
- `create_loan`
- `return_loan`
- `list_member_loans`
- `calculate_late_fee`
- `to_loan_out`

Also separated the internal `get_loan` helper from the response-oriented `get_loan_out` flow so database models can be modified by service operations while API responses use `LoanOut`.

#### Computed loan status

Loan status is calculated at read time:
- `returned` when `returned_at` is set.
- `overdue` when the loan is not returned and `now > due_at`.
- `active` otherwise.

I intentionally did not store the status in the loan table because it is derived state and could become stale as time passes.

#### Late fees

Late fees are charged at 25 cents per started day late, with partial days counting as a full day. The fee is capped at the book's price.

#### Loan transaction safety

Creating and returning loans use transactions around stock and loan updates. This ensures that the database does not retain only part of an operation if a later step fails.

### 9. Top-selling books report

Implemented `top_books`.

The report:
- Counts copies sold using `OrderItem.quantity`.
- Includes only items belonging to paid orders.
- Excludes books with no sales.
- Sorts by copies sold descending.
- Breaks ties by title ascending.
- Applies the requested limit.

The joins follow the actual domain relationships:

```text
Book -> OrderItem -> Order
```

rather than joining orders directly to books.

### 10. Order cancellation and stock restoration

Implemented stock restoration in `cancel_order`.

Initially, cancellation changed only the order status. That left the stock reserved by the cancelled order.

The fix:
- Load all order items.
- Fetch all referenced books in one query.
- Restore the quantity reserved by each order item.
- Mark the order as cancelled.
- Commit the entire operation in one transaction.

I deliberately batch-fetched the books rather than calling `db.get(Book, ...)` inside the loop. This avoids an N+1 query pattern.

## Architecture and Design Decisions

### Service-layer business logic

Business rules are kept in service functions rather than being placed directly in FastAPI routers.

Routers are responsible mainly for:
- Receiving validated request data.
- Providing dependencies such as the database session and current time.
- Calling the appropriate service.
- Returning the service result.

Services contain domain rules such as:
- Duplicate checks.
- Permission checks.
- Stock validation.
- Pricing and discounts.
- Loan limits.
- Late fees.
- Transaction boundaries.

This keeps the API layer thin and makes the business logic easier to test independently.

### Pydantic schemas vs SQLAlchemy models

I kept request/response schemas separate from database models.

For example:
- `OrderCreate` / `OrderItemIn` represent incoming API data.
- `Order` / `OrderItem` represent persisted database state.
- `LoanOut` represents the API representation of a loan.

This separation was particularly useful for loans because `status` is computed when a loan is serialized rather than stored in the database.

### Transactions and data integrity

Operations that modify multiple related pieces of state are performed transactionally.

Examples:
- Creating an order modifies book stock and creates order/order-item records.
- Returning a loan modifies both the loan and book stock.
- Cancelling an order restores multiple books and changes the order status.

The goal is atomicity: either all related database changes are committed or they are rolled back.

### Query efficiency

I paid attention to avoiding unnecessary database round trips.

For example, order creation loads all referenced books with one `IN` query and maps them by ID for validation.

For loan creation, overdue, duplicate-book, and active-loan information is obtained through a single aggregate query instead of issuing a separate query for every check.

For order cancellation, all order items are loaded first and all referenced books are fetched in one query instead of fetching one book per order item.

These choices reduce unnecessary round trips while keeping the service code readable.

### Derived state

Loan status is derived from persisted timestamps instead of stored as a mutable database field.

This avoids a situation where a loan remains marked `active` simply because no background process updated it after its due date.

The trade-off is that the status must be calculated when reading a loan, but this is inexpensive and keeps the state consistent with the timestamps.

## Bugs Found and Fixed

### `tier_at_least` boundary bug

The original implementation used a strict comparison:

```python
return TIER_ORDER.index(tier) > TIER_ORDER.index(minimum)
```

This incorrectly rejected a member whose tier was exactly the required minimum tier.

For example, if `master` is the minimum required tier and the member is `master`, the result should be allowed.

Fixed implementation:

```python
return TIER_ORDER.index(tier) >= TIER_ORDER.index(minimum)
```

The key distinction is between:
- `>`: strictly higher than the required tier.
- `>=`: at least the required tier.

The requirement says "at or above", so `>=` is the correct comparison.

### Search field bug

The initial search implementation checked only `Book.title`. The specification required title OR author matching, so the query was changed to use `or_()`.

### Validation ordering bug

While implementing order creation, I initially combined existence, restriction, and stock checks inside one loop.

That caused a restricted existing book to return `403` before a later nonexistent book could return the required `404`.

The validation was changed into explicit phases:

```text
Check all books exist
        ↓
Check all restricted-book permissions
        ↓
Check all stock
        ↓
Modify stock and create order
```

This matches the specified error precedence.

### N+1 query consideration

For order cancellation, fetching each book inside the order-item loop would result in one query for the order items plus one query per book.

I changed this to:
1. Fetch all order items.
2. Collect their book IDs.
3. Fetch all books with one `IN` query.
4. Build a dictionary keyed by book ID.
5. Restore stock in memory.

This preserves the same behavior while avoiding unnecessary database round trips.

## What I Finished

All provided TODOs were completed.

All initially `NotImplemented` service-layer functions were implemented.

The complete test suite passes, including validation, edge cases, transaction-related behavior, reporting, and the required error precedence.

No assignment TODOs were intentionally left unfinished.

## What I Did Not Implement

No additional features outside the assignment requirements were added.

I did not add a separate migration system because this project uses a local SQLite database for the assignment and the schema changes were handled during development by recreating the local database when necessary.

## Spec Clarifications / Points I Noticed

### Loan status

The specification exposes loan statuses such as `active`, `overdue`, and `returned`, but these are derived from `returned_at`, `due_at`, and the current time.

I treated status as computed state rather than persisted state because storing it would require another mechanism to keep it synchronized as time passes.

### Order validation precedence

The wording around "404 any book not found", "403 any restricted book", and "409 insufficient stock" means the validation phases need to be globally ordered, not just checked item-by-item.

For example, if an order contains one restricted existing book and one nonexistent book, the nonexistent book must result in `404` before the restricted-book check can produce `403`.

## Deployment

Live URL: ______________________________

Deployment platform / database details: ______________________________

## AI Usage

I used AI primarily as a development assistant rather than as a replacement for implementing or verifying the assignment.

### Tools / usage

- **ChatGPT** — used for scaffolding ideas, explaining SQLAlchemy/FastAPI behavior, debugging failing tests, reviewing query structure, and rubber-ducking implementation decisions.
- **Pytest** — used to verify the implementation and identify incorrect assumptions through the actual test suite.
- **Git diff / editor tooling** — used to review incremental changes and catch logic mistakes.

AI was particularly useful for:
- Understanding SQLAlchemy query construction.
- Thinking through transaction boundaries.
- Explaining aggregate queries and conditional aggregation.
- Reviewing edge cases in validation order.
- Debugging failing tests.

### Where I overrode the AI

Where I overrode the AI

During development, I treated AI suggestions as proposals and verified them against the specification and test suite.

One important example was transaction handling. Initially, the AI focused on implementing the business logic but did not account for the need to make multi-step stock and order/loan updates atomic. I identified that operations such as creating an order, returning a loan, and cancelling an order could leave the database in an inconsistent state if a later operation failed. I therefore added explicit transaction handling with rollback so that related changes are committed or reverted together.

I also reviewed an initial approach that used multiple separate database queries for related loan checks and reduced it to a single aggregate query for overdue loans, duplicate unreturned-book detection, and the active-loan count.

Similarly, for order cancellation, I identified the N+1 query problem in fetching books one at a time inside a loop and changed the implementation to batch-fetch all required books with one query.

Overall, I used AI for debugging, reasoning, and rubber-ducking, but verified and modified its suggestions based on the specification, database behavior, query efficiency, and the test suite.
