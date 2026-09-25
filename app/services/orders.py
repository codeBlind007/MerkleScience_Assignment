"""Order operations: placing, paying and cancelling purchases."""
from datetime import datetime
from typing import Dict

from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import select, or_, func, update

from app.services.members import tier_at_least, RESTRICTED_MIN_TIER, get_member
from app.services.books import get_book
from app.models import Member, MemberTier, Order, OrderStatus, Book, OrderItem
from app.schemas import OrderCreate

# Percentage discount granted by each membership tier.
TIER_DISCOUNT_PERCENT: Dict[str, int] = {
    MemberTier.APPRENTICE.value: 0,
    MemberTier.ADEPT.value: 5,
    MemberTier.MASTER.value: 10,
    MemberTier.SUPREME.value: 15,
}

# Extra discount when the total quantity across all items reaches the threshold.
BULK_QUANTITY_THRESHOLD = 10
BULK_DISCOUNT_PERCENT = 5



def calculate_discount_percent(member: Member, total_quantity: int) -> int:
    """Tier discount, plus the bulk discount when total quantity >= threshold."""

    discount_percent = TIER_DISCOUNT_PERCENT[member.tier]

    if total_quantity >= BULK_QUANTITY_THRESHOLD:
        discount_percent += BULK_DISCOUNT_PERCENT

    return discount_percent



def create_order(db: Session, data: OrderCreate, now: datetime) -> Order:
    """Place a pending order and reserve stock.

    Checks, in order (422 for empty items / bad quantity / duplicate books is done by the schema):
    1. 404 member not found; 404 any book not found
    2. 403 any book restricted and member tier below master
    3. 409 any book has insufficient stock (all-or-nothing: nothing is changed)
    Then stock is decremented for every item and prices are snapshotted.
    Pricing: discount_cents = subtotal * percent // 100; total = subtotal - discount.
    """
    # TODO:
    # 1. Load the member (404) and every book (404).
    member = get_member(db, data.member_id)
    book_ids = [item.book_id for item in data.items]
    books = db.scalars(
        select(Book).where(Book.id.in_(book_ids))
    ).all()

    # 2. If any book is restricted, check the member's tier (403).

    books_by_id = {book.id: book for book in books}

    for item in data.items:
        book = books_by_id.get(item.book_id)

        if book is None:
            raise HTTPException(
                status_code=404,
                detail=f"Book {item.book_id} not found"
            )

    for item in data.items:
        book = books_by_id[item.book_id]

        if book.restricted and not tier_at_least(
            member.tier,
            RESTRICTED_MIN_TIER
        ):
            raise HTTPException(
                status_code=403,
                detail="Member is not allowed to order/borrow restricted books"
            )
    # 3. Check stock for every item before changing anything (409).

    for item in data.items:
        book = books_by_id[item.book_id]

        if book.stock < item.quantity:
            raise HTTPException(
                status_code=409,
                detail=f"Insufficient stock for book {book.id}"
            )

    try:
        # 4. Decrement stock and build OrderItems with the current price as unit_price_cents.

        subtotal = 0
        order_items = []
        total_quantity = 0

        for item in data.items:
            book = books_by_id[item.book_id]
            total_quantity += item.quantity
            
            # Optimistic locking check: Ensure stock hasn't changed since it was read
            result = db.execute(
                update(Book)
                .where(Book.id == book.id, Book.stock >= item.quantity)
                .values(stock=Book.stock - item.quantity)
            )
            if result.rowcount == 0:
                raise HTTPException(
                    status_code=409,
                    detail=f"Concurrent modification detected for book {book.id}. Please try again."
                )

            order_item = OrderItem(
                book_id=book.id,
                quantity=item.quantity,
                unit_price_cents=book.price_cents
            )

            subtotal += book.price_cents * item.quantity
            order_items.append(order_item)

        # 5. Compute subtotal, discount_percent (calculate_discount_percent), discount_cents, total.

        discount_percent = calculate_discount_percent(
            member,
            total_quantity
        )

        discount_cents = subtotal * discount_percent // 100

        total_cents = subtotal - discount_cents

        # 6. Save the pending Order with created_at = now and return it.
        order = Order(
            member_id=member.id,
            status=OrderStatus.PENDING.value,
            subtotal_cents=subtotal,
            discount_percent=discount_percent,
            discount_cents=discount_cents,
            total_cents=total_cents,
            created_at=now,
            items=order_items
        )
        db.add(order)
        db.commit()
        db.refresh(order)

        return order

    except Exception:
        db.rollback()
        raise

def get_order(db: Session, order_id: int) -> Order:
    """Return an order by id, or raise 404."""
    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


def pay_order(db: Session, order_id: int) -> Order:
    """Mark a pending order as paid. 404 if missing; 409 if not pending."""
    order = get_order(db, order_id)
    if order.status != OrderStatus.PENDING.value:
        raise HTTPException(status_code=409, detail=f"Cannot pay an order that is {order.status}")
    order.status = OrderStatus.PAID.value
    db.commit()
    db.refresh(order)
    return order


def cancel_order(db: Session, order_id: int) -> Order:
    """Cancel a pending order and restore the reserved stock. 404 if missing; 409 if not pending."""
    order = get_order(db, order_id)

    if order.status != OrderStatus.PENDING.value:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot cancel an order that is {order.status}"
        )

    order_items = db.scalars(
        select(OrderItem).where(OrderItem.order_id == order_id)
    ).all()

    book_ids = [item.book_id for item in order_items]

    books = db.scalars(
        select(Book).where(Book.id.in_(book_ids))
    ).all()

    books_by_id = {book.id: book for book in books}

    try:
        for item in order_items:
            book = books_by_id[item.book_id]
            book.stock += item.quantity

        order.status = OrderStatus.CANCELLED.value

        db.commit()
        db.refresh(order)

        return order

    except Exception:
        db.rollback()
        raise
