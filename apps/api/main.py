from fastapi import FastAPI

app = FastAPI(
    title="TALAP AI Backend",
    version="0.1.0",
    description="Backend and AI orchestration for TALAP.",
)


@app.get("/health/live", tags=["health"])
async def liveness() -> dict[str, str]:
    return {"status": "ok"}