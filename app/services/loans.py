"""Library loan operations: borrowing and returning books."""
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy import and_, select, func, exists
from sqlalchemy.orm import Session

from app.models import Book, Loan, MemberTier, Member, Order, case
from app.services.members import tier_at_least, RESTRICTED_MIN_TIER
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


def loan_status(loan: Loan, now: datetime) -> LoanStatus:
    """``returned`` if returned; else ``overdue`` if now > due_at; else ``active``."""
    status = ""
    if loan.returned_at is not None:
        status = "returned"
    elif loan.due_at < now :
        status = "overdue"
    else: status = "active"

    return status


def to_loan_out(loan: Loan, now: datetime) -> LoanOut:
    """Serialize a loan, computing its status at read time."""
    raise NotImplementedError("to_loan_out")


def calculate_late_fee(due_at: datetime, returned_at: datetime, price_cents: int) -> int:
    """25 cents per started day late (any partial day counts), capped at the book's price; 0 if not late."""
    raise NotImplementedError("calculate_late_fee")


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

    member = db.scalar(select(Member).where(Member.id == data.member_id))
    if member is None:
        raise HTTPException(status_code=404, detail="Member not found")

    book = db.scalar(select(Book).where(Book.id == data.book_id))
    if book is None:
        raise HTTPException(status_code=404, detail="Book not found")

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

    if overdue_count > 0:
        raise HTTPException(
            status_code=409,
            detail="Member has an overdue loan"
        )

    if unreturned_book_count > 0:
        raise HTTPException(
            status_code=409,
            detail="Member already has an unreturned loan for this book"
        )

    if active_loan_count >= TIER_LOAN_LIMIT[member.tier]:
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
            late_fee_cents=0,
            status=LoanStatus.ACTIVE
        )

        db.add(loan)
        db.commit()
        db.refresh(loan)

        return LoanOut.model_validate(loan)

    except Exception:
        db.rollback()
        raise

    

def get_loan(db: Session, loan_id: int, now: datetime) -> LoanOut:
    """Return a loan by id, or raise 404."""
    loan = db.scalar(select(Loan).where(Loan.id == loan_id))
    if loan: 
        raise HTTPException(status_code=404, detail='Loan not found')

    return loan


def return_loan(db: Session, loan_id: int, now: datetime) -> LoanOut:
    """Return a borrowed book.

    Rules: 404 if missing; 409 if already returned. Sets returned_at = now, restores one copy
    of stock and charges a late fee (see ``calculate_late_fee``).
    """
    raise NotImplementedError("return_loan")


def list_member_loans(
    db: Session, member_id: int, now: datetime, status: Optional[LoanStatus] = None
) -> List[LoanOut]:
    """A member's loans ordered by id, optionally filtered by computed status; 404 if member missing."""
    raise NotImplementedError("list_member_loans")
