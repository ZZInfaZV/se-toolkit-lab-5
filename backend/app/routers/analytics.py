"""Router for analytics endpoints.

Each endpoint performs SQL aggregation queries on the interaction data
populated by the ETL pipeline. All endpoints require a `lab` query
parameter to filter results by lab (e.g., "lab-01").
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, case, distinct
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select

from app.database import get_session
from app.models.item import ItemRecord
from app.models.interaction import InteractionLog
from app.models.learner import Learner

router = APIRouter()


@router.get("/scores")
async def get_scores(
    lab: str = Query(..., description="Lab identifier, e.g. 'lab-01'"),
    session: AsyncSession = Depends(get_session),
):
    """Score distribution histogram for a given lab.
    
    - Find the lab item by matching title (e.g. "lab-04" → title contains "Lab 04")
    - Find all tasks that belong to this lab (parent_id = lab.id)
    - Query interactions for these items that have a score
    - Group scores into buckets: "0-25", "26-50", "51-75", "76-100"
      using CASE WHEN expressions
    - Return a JSON array:
      [{"bucket": "0-25", "count": 12}, {"bucket": "26-50", "count": 8}, ...]
    - Always return all four buckets, even if count is 0
    """
    # Extract lab number from parameter (e.g., "lab-04" -> "04")
    lab_number = lab.split('-')[-1]
    
    # Find the lab item by matching title pattern
    lab_stmt = select(ItemRecord).where(
        ItemRecord.type == "lab",
        ItemRecord.title.contains(f"Lab {lab_number}")
    )
    lab_result = await session.execute(lab_stmt)
    lab_item = lab_result.scalar_one_or_none()
    
    if not lab_item:
        # Return empty buckets if lab not found
        return [
            {"bucket": "0-25", "count": 0},
            {"bucket": "26-50", "count": 0},
            {"bucket": "51-75", "count": 0},
            {"bucket": "76-100", "count": 0}
        ]
    
    # Find all tasks belonging to this lab
    tasks_stmt = select(ItemRecord.id).where(
        ItemRecord.parent_id == lab_item.id,
        ItemRecord.type == "task"
    )
    tasks_result = await session.execute(tasks_stmt)
    task_ids = [row[0] for row in tasks_result]
    
    if not task_ids:
        return [
            {"bucket": "0-25", "count": 0},
            {"bucket": "26-50", "count": 0},
            {"bucket": "51-75", "count": 0},
            {"bucket": "76-100", "count": 0}
        ]
    
    # Define score buckets
    bucket_expr = case(
        (InteractionLog.score <= 25, "0-25"),
        (InteractionLog.score <= 50, "26-50"),
        (InteractionLog.score <= 75, "51-75"),
        else_="76-100"
    ).label("bucket")
    
    # Query interactions grouped by score bucket
    stmt = select(
        bucket_expr,
        func.count(InteractionLog.id).label("count")
    ).where(
        InteractionLog.item_id.in_(task_ids),
        InteractionLog.score.isnot(None)
    ).group_by("bucket")
    
    result = await session.execute(stmt)
    rows = result.all()
    
    # Convert to dict for easy lookup
    bucket_counts = {row.bucket: row.count for row in rows}
    
    # Return all buckets in order
    return [
        {"bucket": "0-25", "count": bucket_counts.get("0-25", 0)},
        {"bucket": "26-50", "count": bucket_counts.get("26-50", 0)},
        {"bucket": "51-75", "count": bucket_counts.get("51-75", 0)},
        {"bucket": "76-100", "count": bucket_counts.get("76-100", 0)}
    ]


@router.get("/pass-rates")
async def get_pass_rates(
    lab: str = Query(..., description="Lab identifier, e.g. 'lab-01'"),
    session: AsyncSession = Depends(get_session),
):
    """Per-task pass rates for a given lab.
    
    - Find the lab item and its child task items
    - For each task, compute:
      - avg_score: average of interaction scores (round to 1 decimal)
      - attempts: total number of interactions
    - Return a JSON array:
      [{"task": "Repository Setup", "avg_score": 92.3, "attempts": 150}, ...]
    - Order by task title
    """
    # Extract lab number from parameter
    lab_number = lab.split('-')[-1]
    
    # Find the lab item
    lab_stmt = select(ItemRecord).where(
        ItemRecord.type == "lab",
        ItemRecord.title.contains(f"Lab {lab_number}")
    )
    lab_result = await session.execute(lab_stmt)
    lab_item = lab_result.scalar_one_or_none()
    
    if not lab_item:
        return []
    
    # Query tasks with their interaction statistics
    stmt = select(
        ItemRecord.title.label("task"),
        func.round(func.avg(InteractionLog.score), 1).label("avg_score"),
        func.count(InteractionLog.id).label("attempts")
    ).join(
        InteractionLog, InteractionLog.item_id == ItemRecord.id, isouter=True
    ).where(
        ItemRecord.parent_id == lab_item.id,
        ItemRecord.type == "task"
    ).group_by(
        ItemRecord.id, ItemRecord.title
    ).order_by(
        ItemRecord.title
    )
    
    result = await session.execute(stmt)
    rows = result.all()
    
    return [
        {
            "task": row.task,
            "avg_score": float(row.avg_score) if row.avg_score is not None else 0,
            "attempts": row.attempts
        }
        for row in rows
    ]


@router.get("/timeline")
async def get_timeline(
    lab: str = Query(..., description="Lab identifier, e.g. 'lab-01'"),
    session: AsyncSession = Depends(get_session),
):
    """Submissions per day for a given lab.
    
    - Find the lab item and its child task items
    - Group interactions by date (use func.date(created_at))
    - Count the number of submissions per day
    - Return a JSON array:
      [{"date": "2026-02-28", "submissions": 45}, ...]
    - Order by date ascending
    """
    # Extract lab number from parameter
    lab_number = lab.split('-')[-1]
    
    # Find the lab item
    lab_stmt = select(ItemRecord).where(
        ItemRecord.type == "lab",
        ItemRecord.title.contains(f"Lab {lab_number}")
    )
    lab_result = await session.execute(lab_stmt)
    lab_item = lab_result.scalar_one_or_none()
    
    if not lab_item:
        return []
    
    # Find all tasks belonging to this lab
    tasks_stmt = select(ItemRecord.id).where(
        ItemRecord.parent_id == lab_item.id,
        ItemRecord.type == "task"
    )
    tasks_result = await session.execute(tasks_stmt)
    task_ids = [row[0] for row in tasks_result]
    
    if not task_ids:
        return []
    
    # Group interactions by date
    stmt = select(
        func.date(InteractionLog.created_at).label("date"),
        func.count(InteractionLog.id).label("submissions")
    ).where(
        InteractionLog.item_id.in_(task_ids)
    ).group_by(
        func.date(InteractionLog.created_at)
    ).order_by(
        func.date(InteractionLog.created_at)
    )
    
    result = await session.execute(stmt)
    rows = result.all()
    
    return [
        {
            "date": str(row.date),
            "submissions": row.submissions
        }
        for row in rows
    ]


@router.get("/groups")
async def get_groups(
    lab: str = Query(..., description="Lab identifier, e.g. 'lab-01'"),
    session: AsyncSession = Depends(get_session),
):
    """Per-group performance for a given lab.
    
    - Find the lab item and its child task items
    - Join interactions with learners to get student_group
    - For each group, compute:
      - avg_score: average score (round to 1 decimal)
      - students: count of distinct learners
    - Return a JSON array:
      [{"group": "B23-CS-01", "avg_score": 78.5, "students": 25}, ...]
    - Order by group name
    """
    # Extract lab number from parameter
    lab_number = lab.split('-')[-1]
    
    # Find the lab item
    lab_stmt = select(ItemRecord).where(
        ItemRecord.type == "lab",
        ItemRecord.title.contains(f"Lab {lab_number}")
    )
    lab_result = await session.execute(lab_stmt)
    lab_item = lab_result.scalar_one_or_none()
    
    if not lab_item:
        return []
    
    # Find all tasks belonging to this lab
    tasks_stmt = select(ItemRecord.id).where(
        ItemRecord.parent_id == lab_item.id,
        ItemRecord.type == "task"
    )
    tasks_result = await session.execute(tasks_stmt)
    task_ids = [row[0] for row in tasks_result]
    
    if not task_ids:
        return []
    
    # Query group statistics
    stmt = select(
        Learner.student_group.label("group"),
        func.round(func.avg(InteractionLog.score), 1).label("avg_score"),
        func.count(distinct(Learner.id)).label("students")
    ).join(
        InteractionLog, InteractionLog.learner_id == Learner.id
    ).where(
        InteractionLog.item_id.in_(task_ids),
        InteractionLog.score.isnot(None)
    ).group_by(
        Learner.student_group
    ).order_by(
        Learner.student_group
    )
    
    result = await session.execute(stmt)
    rows = result.all()
    
    return [
        {
            "group": row.group,
            "avg_score": float(row.avg_score) if row.avg_score is not None else 0,
            "students": row.students
        }
        for row in rows
    ]