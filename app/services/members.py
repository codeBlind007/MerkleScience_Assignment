"""Member operations and tier helpers."""
from datetime import datetime
from typing import List

from fastapi import HTTPException
from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session

from app.models import Member, MemberTier, Order, Loan, OrderStatus
from app.schemas import MemberCreate, MemberStats

# Tiers from lowest to highest; a member's rank is their index in this list.
TIER_ORDER: List[str] = [
    MemberTier.APPRENTICE.value,
    MemberTier.ADEPT.value,
    MemberTier.MASTER.value,
    MemberTier.SUPREME.value,
]

# Minimum tier allowed to buy or borrow restricted books.
RESTRICTED_MIN_TIER = MemberTier.MASTER.value


def tier_at_least(tier: str, minimum: str) -> bool:
    """True if ``tier`` ranks at or above ``minimum``."""
    return TIER_ORDER.index(tier) >= TIER_ORDER.index(minimum)


def ensure_can_access_restricted(member: Member) -> None:
    """Raise 403 unless the member's tier may access restricted books."""
    if not tier_at_least(member.tier, RESTRICTED_MIN_TIER):
        raise HTTPException(
            status_code=403, detail=f"Restricted books require tier '{RESTRICTED_MIN_TIER}' or higher"
        )


def get_all_members(db: Session, page: int, limit: int) -> List[Member]:
    offset = (page-1) * limit
    return db.scalars(
        select(Member)
        .order_by(Member.id)
        .offset(offset)
        .limit(limit)
    ).all()

def create_member(db: Session, data: MemberCreate, now: datetime) -> Member:
    """Register a member.

    Rules: email (already stripped + lowercased) must be unique -> 409; created_at = now.
    """
    # TODO: reject an email that is already in use with 409
    existing_member = db.scalar(select(Member).where(Member.email == data.email))

    if existing_member:
        raise HTTPException(status_code = 409, detail = "Email already in use")

    member = Member(name=data.name, email=data.email, tier=data.tier.value, created_at=now)
    db.add(member)
    db.commit()
    db.refresh(member)
    return member


def get_member(db: Session, member_id: int) -> Member:
    """Return a member by id, or raise 404."""
    member = db.get(Member, member_id)
    if member is None:
        raise HTTPException(status_code=404, detail="Member not found")
    return member



def list_member_orders(db: Session, member_id: int) -> List[Order]:
    """All orders of a member ordered by id ascending; 404 if the member is missing."""
    get_member(db, member_id)
    return list(db.scalars(select(Order).where(Order.member_id == member_id).order_by(Order.id.asc())))


def get_member_stats(db: Session, member_id: int, now: datetime) -> MemberStats:
    """Summarize a member's activity.

    Rules:
    - 404 if the member is missing.
    - orders_paid / total_spent_cents consider only ``paid`` orders.
    - active_loans counts every unreturned loan (overdue ones included).
    - overdue_loans counts unreturned loans with now > due_at.
    - late_fees_cents sums late fees of returned loans.
    """

    get_member(db, member_id);

    orders_paid, total_spent_cents = db.execute(
    select(
        func.count(Order.id),
        func.coalesce(func.sum(Order.total_cents), 0)
    ).where(
        Order.member_id == member_id,
        Order.status == OrderStatus.PAID
    )
    ).one()

    active_loans, overdue_loans, late_fees_cents = db.execute(
        select(
            func.coalesce(
                func.sum(
                    case(
                        (Loan.returned_at.is_(None), 1),
                        else_=0
                    )
                ),
                0
            ),
            func.coalesce(
                func.sum(
                    case(
                        (
                            (Loan.returned_at.is_(None)) & (Loan.due_at < now),
                            1
                        ),
                        else_=0
                    )
                ),
                0
            ),
            func.coalesce(
                func.sum(
                    case(
                        (
                            Loan.returned_at.is_not(None),
                            Loan.late_fee_cents
                        ),
                        else_=0
                    )
                ),
                0
            )
        ).where(
            Loan.member_id == member_id
        )
    ).one()

    return MemberStats(
        member_id=member_id,
        orders_paid=orders_paid,
        total_spent_cents=total_spent_cents,
        active_loans=active_loans,
        overdue_loans=overdue_loans,
        late_fees_cents=late_fees_cents,
    )
    
    
    
