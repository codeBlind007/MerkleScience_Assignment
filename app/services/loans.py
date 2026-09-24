"""Library loan operations: borrowing and returning books."""
from datetime import datetime, timedelta
from math import ceil
from typing import Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy import and_, select, func, exists, case
from sqlalchemy.orm import Session

from app.models import Book, Loan, MemberTier, Member, Order
from app.services.members import tier_at_least, RESTRICTED_MIN_TIER, get_member
from app.services.books import get_book
from app.schemas import LoanCreate, LoanOut, LoanStatus

# Maximum concurrent unreturned loans per tier (None = unlimited).
TIER_LOAN_LIMIT: Dict[str, Optional[int]] = {
    MemberTier.APPRENTICE.value: 1,
    MemberTier.ADEPT.value: 3,
    MemberTier.MASTER.value: 5,
    MemberTier.SUPREME.value: None,
}

LOAN_PERIOD = timedelta(days=14)
LATE_FEE_PER_DAY_CENTS = 25

# internal DB getter

def get_loan(db: Session, loan_id: int) -> Loan:
    loan = db.scalar(select(Loan).where(Loan.id == loan_id))

    if loan is None:
        raise HTTPException(status_code=404, detail="Loan not found")

    return loan

def loan_status(loan: Loan, now: datetime) -> LoanStatus:
    """``returned`` if returned; else ``overdue`` if now > due_at; else ``active``."""
    if loan.returned_at is not None:
        return "returned"
    elif loan.due_at < now:
        return "overdue"
    else:
        return "active"


def to_loan_out(loan: Loan, now: datetime) -> LoanOut:
    """Serialize a loan, computing its status at read time."""
    return LoanOut(
        id=loan.id,
        member_id=loan.member_id,
        book_id=loan.book_id,
        borrowed_at=loan.borrowed_at,
        due_at=loan.due_at,
        returned_at=loan.returned_at,
        late_fee_cents=loan.late_fee_cents,
        status=loan_status(loan, now)
    )


def calculate_late_fee(due_at: datetime, returned_at: datetime, price_cents: int) -> int:
    """25 cents per started day late (any partial day counts), capped at the book's price; 0 if not late."""

    if due_at >= returned_at: return 0

    late_seconds = (returned_at - due_at).total_seconds()
    late_days = ceil(late_seconds / (24 * 60 * 60))

    return min(late_days * 25, price_cents)



def create_loan(db: Session, data: LoanCreate, now: datetime) -> LoanOut:
    """Borrow a book for 14 days.

    Checks, in order:
    1. 404 member not found; 404 book not found
    2. 403 book restricted and member tier below master
    3. 409 member has any overdue loan
    4. 409 member already has an unreturned loan of this book
    5. 409 member is at their tier's loan limit
    6. 409 book is out of stock
    On success: borrowed_at = now, due_at = now + 14 days, returned_at None,
    late_fee_cents 0, and stock is decremented by one.
    """

    member = get_member(db, data.member_id)
    book = get_book(db, data.book_id)

    if book.restricted and not tier_at_least(
        member.tier,
        RESTRICTED_MIN_TIER
    ):
        raise HTTPException(
            status_code=403,
            detail="Member is not allowed to borrow restricted books"
        )

    overdue_count, unreturned_book_count, active_loan_count = db.execute(
        select(
            func.sum(
                case(
                    (
                        and_(
                            Loan.returned_at.is_(None),
                            Loan.due_at < now
                        ),
                        1
                    ),
                    else_=0
                )
            ),
            func.sum(
                case(
                    (
                        and_(
                            Loan.book_id == data.book_id,
                            Loan.returned_at.is_(None)
                        ),
                        1
                    ),
                    else_=0
                )
            ),
            func.sum(
                case(
                    (
                        Loan.returned_at.is_(None),
                        1
                    ),
                    else_=0
                )
            )
        ).where(
            Loan.member_id == data.member_id
        )
    ).one()

    if overdue_count is not None and overdue_count > 0:
        raise HTTPException(
            status_code=409,
            detail="Member has an overdue loan"
        )

    if unreturned_book_count is not None and unreturned_book_count > 0:
        raise HTTPException(
            status_code=409,
            detail="Member already has an unreturned loan for this book"
        )

    if (
        TIER_LOAN_LIMIT[member.tier] is not None
        and active_loan_count is not None
        and active_loan_count >= TIER_LOAN_LIMIT[member.tier]
    ):
        raise HTTPException(
            status_code=409,
            detail="Member has reached their loan limit"
        )

    if book.stock <= 0:
        raise HTTPException(
            status_code=409,
            detail="Book is out of stock"
        )

    try:
        book.stock -= 1

        loan = Loan(
            member_id=member.id,
            book_id=book.id,
            borrowed_at=now,
            due_at=now + LOAN_PERIOD,
            returned_at=None,
            late_fee_cents=0
        )

        db.add(loan)
        db.commit()
        db.refresh(loan)

        return to_loan_out(loan, now)

    except Exception:
        db.rollback()
        raise

# api facing getter

def get_loan_out(db: Session, loan_id: int, now: datetime) -> LoanOut:
    loan = get_loan(db, loan_id)
    return to_loan_out(loan, now)


def return_loan(db: Session, loan_id: int, now: datetime) -> LoanOut:
    """Return a borrowed book.

    Rules: 404 if missing; 409 if already returned. Sets returned_at = now, restores one copy
    of stock and charges a late fee (see ``calculate_late_fee``).
    """
    loan = get_loan(db, loan_id)
    if loan.returned_at is not None:
        raise HTTPException(status_code = 409, detail='Loan already returned')

    book = get_book(db, loan.book_id)

    try:
        loan.returned_at = now
        book.stock += 1
        loan.late_fee_cents = calculate_late_fee(loan.due_at, now, book.price_cents)

        db.commit()
        db.refresh(loan)
        db.refresh(book)

        return to_loan_out(loan, now)
    except Exception:
        db.rollback()
        raise


def list_member_loans(
    db: Session, member_id: int, now: datetime, status: Optional[LoanStatus] = None
) -> List[LoanOut]:
    """A member's loans ordered by id, optionally filtered by computed status; 404 if member missing."""

    get_member(db, member_id)

    loans = db.scalars(
        select(Loan)
        .where(Loan.member_id == member_id)
        .order_by(Loan.id.asc())
    ).all()

    result = []

    for loan in loans:
        computed_status = loan_status(loan, now)

        if status is not None and computed_status != status:
            continue

        result.append(to_loan_out(loan, now))

    return result
