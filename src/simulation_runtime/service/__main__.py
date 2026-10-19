"""Start the HTTP service: ``python -m simulation_runtime.service``."""

from __future__ import annotations

import os


def main() -> None:
    import uvicorn

    from .app import create_app
    from .config import load_settings

    settings = load_settings()
    host = os.environ.get("SIM_RUNTIME_HOST", "127.0.0.1")
    port = int(os.environ.get("SIM_RUNTIME_PORT", "8080"))
    log_level = os.environ.get("SIM_RUNTIME_LOG_LEVEL", "info")
    forwarded = os.environ.get("SIM_RUNTIME_FORWARDED_ALLOW_IPS") or None
    uvicorn.run(
        create_app(settings),
        host=host,
        port=port,
        log_level=log_level,
        proxy_headers=forwarded is not None,
        forwarded_allow_ips=forwarded,
    )


if __name__ == "__main__":
    main()
