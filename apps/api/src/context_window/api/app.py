"""The HTTP API (PLAN.md 8), deployed on Vercel. It reads the database and starts
newsroom jobs on GitHub Actions; it never runs the graph itself."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from context_window import __version__
from context_window.api import briefings, health, runs, settings_routes, stories
from context_window.config import get_settings


def create_app() -> FastAPI:
    app = FastAPI(title="Context Window", version=__version__)
    origin = get_settings().web_origin
    if origin:
        # Only the web app may call the API from a browser.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[origin.rstrip("/")],
            allow_methods=["GET", "POST", "PUT"],
            allow_headers=["Authorization", "Content-Type"],
        )
    for module in (health, briefings, runs, stories, settings_routes):
        app.include_router(module.router)
    return app


app = create_app()
