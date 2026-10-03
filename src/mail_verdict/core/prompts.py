"""
Jinja2 prompt template loader.

Loads .md.j2 templates from config/prompts/ and renders them
with provided variables. Tries development path first, then
container path (/app/config/prompts/).
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import BaseLoader, Environment, TemplateNotFound

logger = logging.getLogger(__name__)

_PROMPT_DIRS = [
    Path(__file__).parent.parent.parent.parent / "config" / "prompts",
    Path("/app/config/prompts"),
]


class _MultiPathLoader(BaseLoader):
    """Jinja2 loader that searches multiple directories."""

    def __init__(self, search_paths: list[Path]) -> None:
        """
        Initialize with search paths.

        Args:
            search_paths: Directories to search for templates
        """
        self.search_paths = search_paths

    def get_source(
        self,
        environment: Environment,
        template: str,
    ) -> tuple[str, str, Any]:
        """
        Load a template from the first matching path.

        Args:
            environment: Jinja2 environment
            template: Template filename

        Returns:
            Tuple of (source, filename, uptodate_callable)

        Raises:
            TemplateNotFound: If template not found in any path
        """
        for search_path in self.search_paths:
            path = search_path / template
            if path.exists():
                source = path.read_text()
                return source, str(path), lambda: True
        raise TemplateNotFound(template)


@lru_cache(maxsize=1)
def _get_env() -> Environment:
    """Get the cached Jinja2 environment."""
    return Environment(
        loader=_MultiPathLoader(_PROMPT_DIRS),
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_prompt(template_name: str, **variables: Any) -> str:
    """
    Render a prompt template with the given variables.

    Args:
        template_name: Template filename (e.g., "spam_system.md.j2")
        **variables: Template variables

    Returns:
        Rendered prompt string

    Raises:
        jinja2.TemplateNotFound: If template file not found
    """
    env = _get_env()
    template = env.get_template(template_name)
    return template.render(**variables).strip()


def load_static_prompt(template_name: str) -> str:
    """
    Load a static prompt template (no variables needed).

    Args:
        template_name: Template filename

    Returns:
        Prompt content string
    """
    return render_prompt(template_name)


def escape_delimiter_breakout(json_text: str) -> str:
    """Replace every `<` and `>` with its `\\uXXXX` escape.

    Still valid JSON -- a unicode escape inside a string literal decodes
    to the same character -- but the delimiter tags the untrusted content
    is fenced in (spam_user.md.j2's `<email_content>`/`</email_content>`)
    can no longer be spelled inside it, so a message body containing the
    literal closing tag can no longer close the fence early. `json.dumps`
    itself has no option for this: it escapes quotes and backslashes,
    never angle brackets.
    """
    return json_text.replace("<", "\\u003c").replace(">", "\\u003e")
