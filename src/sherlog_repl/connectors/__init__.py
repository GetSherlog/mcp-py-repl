"""
Connectors package for the Sherlog REPL.
Provides interfaces to external MCP services.
"""

from .base import MCPConnector
from .github import GitHubConnector
from .filesystem import FilesystemConnector
from .jira import JiraConnector

__all__ = [
    'MCPConnector',
    'GitHubConnector',
    'FilesystemConnector',
    'JiraConnector',
]