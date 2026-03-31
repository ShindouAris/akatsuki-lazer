"""Friends endpoints."""

from fastapi import APIRouter
from fastapi import status
from sqlalchemy import and_
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.api.deps import DbSession
from app.api.v2.schemas import Relation, UserCompact
from app.api.v2.schemas import UserRelationResponse
from app.core.error import OsuError
from app.models.user import User
from app.models.user import UserRelation

router = APIRouter()


async def _get_mutual_friend_ids(db: AsyncSession, user_id: int) -> set[int]:
    """Get IDs of users who have a mutual friendship with the given user."""
    # Find all users that the current user has friended who also have friended back
    result = await db.execute(
        select(UserRelation.target_id)
        .where(
            and_(
                UserRelation.user_id == user_id,
                UserRelation.friend == True,  # noqa: E712
            ),
        )
        .intersect(
            select(UserRelation.user_id).where(
                and_(
                    UserRelation.target_id == user_id,
                    UserRelation.friend == True,  # noqa: E712
                ),
            ),
        ),
    )
    return {row[0] for row in result.fetchall()}


@router.get("/friends")
async def get_friends(user: CurrentUser, db: DbSession) -> list[Relation]:
    """Get user's friends list."""
    # Get all friend relations for this user
    result = await db.execute(
        select(UserRelation, User)
        .join(User, User.id == UserRelation.target_id)
        .where(
            and_(
                UserRelation.user_id == user.id,
                UserRelation.friend == True,  # noqa: E712
                User.is_active == True,  # noqa: E712
            ),
        ),
    )
    relations: list[tuple[UserRelation, User]] = result.fetchall()

    # Get mutual friend IDs
    mutual_ids = await _get_mutual_friend_ids(db, user.id)

    friends = []
    for relation, target_user in relations:
        friends.append(
            Relation(
                    target_id=relation.target_id,
                    relation_type="friend",
                    mutual=relation.target_id in mutual_ids,
                    target=UserCompact.model_validate(target_user),
            )
        )

    print(friends)

    return friends


@router.post("/friends")
async def add_friend(
    target: int,
    user: CurrentUser,
    db: DbSession,
) -> UserRelationResponse:
    """Add a user as a friend."""
    target_id = target

    # Can't friend yourself
    if target_id == user.id:
        raise OsuError(
            code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            error="Cannot add yourself as a friend",
            message="Cannot add yourself as a friend",
        )

    # Check if target user exists and is active
    target_user = await db.get(User, target_id)
    if not target_user or not target_user.is_active:
        raise OsuError(
            code=status.HTTP_404_NOT_FOUND,
            error="User not found",
            message="User not found",
        )

    # Check friend limit
    friend_count_result = await db.execute(
        select(UserRelation)
        .where(
            and_(
                UserRelation.user_id == user.id,
                UserRelation.friend == True,  # noqa: E712
            ),
        ),
    )
    friend_count = len(friend_count_result.fetchall())

    if friend_count >= user.max_friends:
        raise OsuError(
            code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            error=f"Friend limit reached ({user.max_friends})",
            message=f"Friend limit reached ({user.max_friends})",
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
        if relation.friend:
            raise OsuError(
                code=status.HTTP_403_FORBIDDEN,
                error="Already friends",
                hint="The specified user is already in your friends list.",
                message="Friend relation already exists"
            )
        # Was blocked, now becoming friend - remove block
        relation.friend = True
        relation.foe = False
    else:
        relation = UserRelation(
            user_id=user.id,
            target_id=target_id,
            friend=True,
            foe=False,
        )
        db.add(relation)

    await db.commit()

    # Check if mutual
    mutual_result = await db.execute(
        select(UserRelation).where(
            and_(
                UserRelation.user_id == target_id,
                UserRelation.target_id == user.id,
                UserRelation.friend == True,  # noqa: E712
            ),
        ),
    )
    is_mutual = mutual_result.scalar_one_or_none() is not None

    return {
        "user_relation": Relation(
            target_id=target_id,
            relation_type="friend",
            mutual=is_mutual,
            target=UserCompact.model_validate(target_user),
        )
    } 


@router.delete("/friends/{target_id}")
async def remove_friend(
    target_id: int,
    user: CurrentUser,
    db: DbSession,
) -> dict:
    """Remove a friend."""
    # Find the relation
    result = await db.execute(
        select(UserRelation).where(
            and_(
                UserRelation.user_id == user.id,
                UserRelation.target_id == target_id,
                UserRelation.friend == True,  # noqa: E712
            ),
        ),
    )
    relation = result.scalar_one_or_none()

    if not relation:
        raise OsuError(
            code=status.HTTP_404_NOT_FOUND,
            error="Friend not found",
            message="Friend not found",
        )

    # If they're also blocked, just remove friend status; otherwise delete
    if relation.foe:
        relation.friend = False
    else:
        await db.delete(relation)

    await db.commit()

    return {}
