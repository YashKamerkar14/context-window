from fastapi import FastAPI

from context_window import __version__
from context_window.api import health


def create_app() -> FastAPI:
    app = FastAPI(title="Context Window", version=__version__)
    app.include_router(health.router)
    return app


app = create_app()
