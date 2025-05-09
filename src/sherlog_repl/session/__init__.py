"""
Session package for the Sherlog REPL.
Manages user sessions and their state.
"""

from .manager import Session, SessionManager

__all__ = [
    'Session',
    'SessionManager',
]