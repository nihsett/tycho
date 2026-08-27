"""ASGI entrypoint: ``uvicorn dashboard.api.main:app``."""

from dashboard.api.app import create_app

app = create_app()
