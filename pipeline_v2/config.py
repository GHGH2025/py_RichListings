"""
PipelineV2 configuration helpers.

Toggle:
    PIPELINE_VERSION=v1   (default) – run existing cron-based jobs
    PIPELINE_VERSION=v2             – run the single end-to-end pipeline worker

Worker pool:
    PIPELINE_V2_WORKERS=2       Number of concurrent email workers (default 2)
    PIPELINE_V2_POLL_SECONDS=5  How often the runner polls for new emails (default 5s)
"""
import os


def get_pipeline_version() -> str:
    return os.getenv("PIPELINE_VERSION", "v1").lower().strip()


def is_v2() -> bool:
    return get_pipeline_version() == "v2"


def get_v2_workers() -> int:
    return int(os.getenv("PIPELINE_V2_WORKERS", "2"))


def get_v2_poll_seconds() -> float:
    return float(os.getenv("PIPELINE_V2_POLL_SECONDS", "5"))
