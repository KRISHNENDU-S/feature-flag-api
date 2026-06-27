"""FastAPI application wiring together storage, cache and evaluation."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Response, status

from app.cache.cache import TTLCache
from app.config import settings
from app.logging_config import configure_logging, get_logger
from app.models import (
    EvaluationRequest,
    EvaluationResult,
    Flag,
    FlagCreate,
    FlagUpdate,
)
from app.services.evaluation_service import EvaluationService
from app.storage.storage import FlagStorage, StorageUnavailableError

configure_logging(settings.log_level)
logger = get_logger("app.api")

# Singletons for the process lifetime.
cache = TTLCache(ttl_seconds=settings.cache_ttl_seconds)
storage = FlagStorage()
evaluation_service = EvaluationService(storage=storage, cache=cache)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("service starting", extra={"context": {"app": settings.app_name}})
    yield
    logger.info("service stopping")


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description="A feature flag service with rule-based and percentage rollouts.",
    lifespan=lifespan,
)


# -- dependency providers (overridable in tests) --------------------------
def get_storage() -> FlagStorage:
    return storage


def get_cache() -> TTLCache:
    return cache


def get_evaluation_service() -> EvaluationService:
    return evaluation_service


# -- endpoints ------------------------------------------------------------
@app.post("/flags", response_model=Flag, status_code=status.HTTP_201_CREATED)
async def create_flag(
    payload: FlagCreate,
    store: FlagStorage = Depends(get_storage),
) -> Flag:
    flag = Flag(**payload.model_dump())
    try:
        created = await store.create(flag)
    except ValueError as exc:
        logger.warning("flag create conflict", extra={"context": {"name": flag.name}})
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except StorageUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    return created


@app.get("/flags", response_model=list[Flag])
async def list_flags(
    name: str | None = Query(
        default=None, description="Case-insensitive partial name match."
    ),
    default_state: bool | None = Query(
        default=None, description="Filter by default state (true/false)."
    ),
    store: FlagStorage = Depends(get_storage),
) -> list[Flag]:
    return await store.list(name=name, default_state=default_state)


@app.get("/flags/{flag_id}", response_model=Flag)
async def get_flag(
    flag_id: str,
    store: FlagStorage = Depends(get_storage),
) -> Flag:
    flag = await store.get(flag_id)
    if flag is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="flag not found"
        )
    return flag


@app.patch("/flags/{flag_id}", response_model=Flag)
async def update_flag(
    flag_id: str,
    payload: FlagUpdate,
    store: FlagStorage = Depends(get_storage),
    flag_cache: TTLCache = Depends(get_cache),
) -> Flag:
    # Only the fields the client actually sent (keeps Rule objects typed and
    # lets an explicit `null` percentage clear the rollout).
    changes = {field: getattr(payload, field) for field in payload.model_fields_set}
    try:
        updated = await store.update(flag_id, changes)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="flag not found"
        ) from exc
    except ValueError as exc:
        logger.warning("flag update conflict", extra={"context": {"flag_id": flag_id}})
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except StorageUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    # Stale evaluations for this flag must not survive a change.
    flag_cache.invalidate(flag_id)
    return updated


@app.delete("/flags/{flag_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_flag(
    flag_id: str,
    store: FlagStorage = Depends(get_storage),
    flag_cache: TTLCache = Depends(get_cache),
) -> Response:
    deleted = await store.delete(flag_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="flag not found"
        )
    # Drop any cached evaluations for this flag.
    flag_cache.invalidate(flag_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/flags/{flag_id}/evaluate", response_model=EvaluationResult)
async def evaluate_flag(
    flag_id: str,
    request: EvaluationRequest,
    service: EvaluationService = Depends(get_evaluation_service),
) -> EvaluationResult:
    try:
        return await service.evaluate(flag_id, request)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="flag not found"
        ) from exc


@app.get("/health")
async def health(
    store: FlagStorage = Depends(get_storage),
    flag_cache: TTLCache = Depends(get_cache),
) -> dict[str, object]:
    return {
        "status": "ok",
        "total_flags": await store.count(),
        "cache": flag_cache.stats(),
    }
