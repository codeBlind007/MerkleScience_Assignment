"""Book catalogue operations."""
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select, or_, func
from sqlalchemy.orm import Session
from app.models import Book
from app.schemas import BookCreate, BookPage, BookSort, BookUpdate


def create_book(db: Session, data: BookCreate) -> Book:
    """Add a book to the catalogue.

    Rules: the (already normalized) ISBN must be unique -> 409 otherwise.
    """
    # TODO: reject a duplicate ISBN with 409
    book = Book(**data.model_dump())
    existing_book = db.scalar(select(Book).where(Book.isbn == book.isbn))

    if existing_book:
        raise HTTPException(status_code=409, detail="Book with this ISBN already exist")

    db.add(book)
    db.commit()
    db.refresh(book)
    return book


def get_book(db: Session, book_id: int) -> Book:
    """Return a book by id, or raise 404."""
    book = db.get(Book, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Book not found")
    return book


def update_book(db: Session, book_id: int, data: BookUpdate) -> Book:
    """Apply a partial update. Only fields present in the request are changed; 404 if missing."""
    book = db.get(Book, book_id)
    if book is None:
        raise HTTPException(status_code = 404, detail="Book not found")

    if data.title is not None:
        book.title = data.title
    if data.author is not None:
        book.author = data.author
    if data.price_cents is not None:
        book.price_cents = data.price_cents
    if data.stock is not None:
        book.stock = data.stock
    if data.restricted is not None:
        book.restricted = data.restricted

    db.commit()
    db.refresh(book)

    return book


def list_books(
    db: Session,
    q: Optional[str] = None,
    restricted: Optional[bool] = None,
    min_price: Optional[int] = None,
    max_price: Optional[int] = None,
    sort: Optional[BookSort] = None,
    limit: int = 20,
    offset: int = 0,
) -> BookPage:
    """Search the catalogue.

    Rules:
    - ``q`` matches title OR author, case-insensitive substring.
    - ``restricted`` filters exactly; ``min_price``/``max_price`` are inclusive.
    - Sorted by ``sort`` (title / price, ``-`` for descending) with ties broken by id;
      default order is id ascending.
    - ``total`` counts all matches before ``limit``/``offset`` are applied.
    """
    query = select(Book)
    if q:
        query = query.where(or_(
            Book.title.icontains(q, autoescape=True),
            Book.author.icontains(q, autoescape=True)
        ))
    if restricted is not None:
        query = query.where(Book.restricted == restricted)
    # TODO: min_price / max_price filters

    if min_price is not None: 
        query = query.where(Book.price_cents >= min_price)

    if max_price is not None:
        query = query.where(Book.price_cents <= max_price)

    # TODO: apply ``sort``
    if sort == 'title':
        query = query.order_by(Book.title.asc(), Book.id.asc())
    elif sort == '-title':
        query = query.order_by(Book.title.desc(), Book.id.asc())
    elif sort == 'price':
        query = query.order_by(Book.price_cents.asc(), Book.id.asc())
    elif sort == '-price':
        query = query.order_by(Book.price_cents.desc(), Book.id.asc())
    else:
        query = query.order_by(Book.id.asc())
    
    total = db.scalar(
        select(func.count()).select_from(query.order_by(None).subquery())
    )

    books = db.scalars(
        query.limit(limit).offset(offset)
    ).all()

    return BookPage(
        items=books,
        total=total,
        limit=limit,
        offset=offset
    )
