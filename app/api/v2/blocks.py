"""Blocks endpoints."""

from fastapi import APIRouter
from fastapi import status
from sqlalchemy import and_
from sqlalchemy import select

from app.api.deps import CurrentUser
from app.api.deps import DbSession
from app.api.v2.schemas import UserCompact
from app.api.v2.schemas import UserRelationResponse
from app.core.error import OsuError
from app.models.user import User
from app.models.user import UserRelation

router = APIRouter()


@router.get("/blocks")
async def get_blocks(user: CurrentUser, db: DbSession) -> list[UserRelationResponse]:
    """Get user's block list."""
    # Get all block relations for this user
    result = await db.execute(
        select(UserRelation, User)
        .join(User, User.id == UserRelation.target_id)
        .where(
            and_(
                UserRelation.user_id == user.id,
                UserRelation.foe == True,  # noqa: E712
                User.is_active == True,  # noqa: E712
            ),
        ),
    )
    relations = result.fetchall()

    blocks = []
    for relation, target_user in relations:
        blocks.append(
            UserRelationResponse(
                target_id=relation.target_id,
                relation_type="block",
                mutual=False,  # Blocks are never mutual in display
                target=UserCompact.model_validate(target_user),
            ),
        )

    return blocks


@router.post("/blocks")
async def add_block(
    target_id: int,
    user: CurrentUser,
    db: DbSession,
) -> UserRelationResponse:
    """Block a user."""
    # Can't block yourself
    if target_id == user.id:
        raise OsuError(
            code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            error="Cannot block yourself",
            message="Cannot block yourself",
        )

    # Check if target user exists and is active
    target = await db.get(User, target_id)
    if not target or not target.is_active:
        raise OsuError(
            code=status.HTTP_404_NOT_FOUND,
            error="User not found",
            message="User not found",
        )

    # Check block limit
    block_count_result = await db.execute(
        select(UserRelation)
        .where(
            and_(
                UserRelation.user_id == user.id,
                UserRelation.foe == True,  # noqa: E712
            ),
        ),
    )
    block_count = len(block_count_result.fetchall())

    if block_count >= user.max_blocks:
        raise OsuError(
            code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            error=f"Block limit reached ({user.max_blocks})",
            message=f"Block limit reached ({user.max_blocks})",
        )

    # Check if relation already exists
    existing = await db.execute(
        select(UserRelation).where(
            and_(
                UserRelation.user_id == user.id,
                UserRelation.target_id == target_id,
            ),
        ),
    )
    relation = existing.scalar_one_or_none()

    if relation:
        if relation.foe:
            raise OsuError(
                code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                error="Already blocking this user",
                message="Already blocking this user",
            )
        # Was friend, now blocking - remove friend status
        relation.friend = False
        relation.foe = True
    else:
        relation = UserRelation(
            user_id=user.id,
            target_id=target_id,
            friend=False,
            foe=True,
        )
        db.add(relation)

    await db.commit()

    return UserRelationResponse(
        target_id=target_id,
        relation_type="block",
        mutual=False,
        target=UserCompact.model_validate(target),
    )


@router.delete("/blocks/{target_id}")
async def remove_block(
    target_id: int,
    user: CurrentUser,
    db: DbSession,
) -> dict:
    """Unblock a user."""
    # Find the relation
    result = await db.execute(
        select(UserRelation).where(
            and_(
                UserRelation.user_id == user.id,
                UserRelation.target_id == target_id,
                UserRelation.foe == True,  # noqa: E712
            ),
        ),
    )
    relation = result.scalar_one_or_none()

    if not relation:
        raise OsuError(
            code=status.HTTP_404_NOT_FOUND,
            error="Block not found",
            message="Block not found",
        )

    # If they're also a friend (shouldn't happen but handle it), just remove block
    if relation.friend:
        relation.foe = False
    else:
        await db.delete(relation)

    await db.commit()

    return {}
