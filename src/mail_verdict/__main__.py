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
    )
