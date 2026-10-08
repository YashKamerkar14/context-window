from fastapi import APIRouter
from pydantic import BaseModel

from context_window import __version__

router = APIRouter()


class HealthResponse(BaseModel):
    status: str
    version: str


@router.get("/health")
def health() -> HealthResponse:
    return HealthResponse(status="ok", version=__version__)
