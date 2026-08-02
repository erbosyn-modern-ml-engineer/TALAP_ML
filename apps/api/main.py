from fastapi import FastAPI

from apps.api.routes import catalog_router, telegram_webhook_router

app = FastAPI(
    title="TALAP AI Backend",
    version="0.1.0",
    description="Backend and AI orchestration for TALAP.",
)


@app.get("/health/live", tags=["health"])
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(catalog_router)
# Registered separately (no /api/v1 prefix) so the external webhook path is
# exactly /webhooks/telegram/{connection_id}.
app.include_router(telegram_webhook_router)