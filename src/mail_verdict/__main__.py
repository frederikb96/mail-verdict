"""
MailVerdict entry point.

Run with: python -m mail_verdict
"""

if __name__ == "__main__":
    from mail_verdict.config import get_config

    config = get_config()

    from mail_verdict.core.logging import setup_logging

    setup_logging(config.server.log_level)

    import uvicorn

    uvicorn.run(
        "mail_verdict.server:create_app",
        host=config.server.host,
        port=config.server.port,
        log_config=None,
        factory=True,
        # An SSE stream stays open for as long as its client is connected, and
        # a graceful shutdown waits for open connections. Without a bound, a
        # process with a browser attached never exits on its own and is killed
        # by whatever is supervising it once its grace period runs out.
        timeout_graceful_shutdown=10,
    )
