"""
Sherlog-Canvas Unified REPL MCP Server.

A persistent Python REPL with access to multiple MCP tools,
including GitHub, Filesystem, and Jira.
"""

from . import server
from . import connectors
from . import session
from . import proxy
from . import utils

__version__ = "0.1.0"


def main():
    """Main entry point for the package."""
    return server.main()


__all__ = [
    'main',
    'server',
    'connectors',
    'session',
    'proxy',
    'utils',
]