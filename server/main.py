"""KiteCLI FastAPI application entry point.

Configures CORS, authentication middleware, health check,
and includes the API router.
"""

import logging
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from routes import router

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="KiteCLI Server",
    description="Backend server for the KiteCLI tool — proxies Zerodha Kite Connect API.",
    version="0.1.0",
)

# ---------------------------------------------------------------------------
# CORS — allow all origins for development
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Auth middleware — validates X-Auth-Token header against AUTH_TOKEN env var
# ---------------------------------------------------------------------------
AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Check X-Auth-Token header for all routes except /health.

    If AUTH_TOKEN env var is not set, authentication is skipped entirely
    so the server can run without configuration during development.
    """
    if request.url.path == "/health":
        return await call_next(request)

    if AUTH_TOKEN:
        token = request.headers.get("X-Auth-Token", "")
        if token != AUTH_TOKEN:
            logger.warning(
                "Unauthorized request to %s from %s",
                request.url.path,
                request.client.host if request.client else "unknown",
            )
            return JSONResponse(
                status_code=401,
                content={"detail": "Unauthorized — invalid or missing X-Auth-Token"},
            )

    return await call_next(request)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    """Simple health check endpoint."""
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Include API routes
# ---------------------------------------------------------------------------
app.include_router(router)

# ---------------------------------------------------------------------------
# Dev entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
