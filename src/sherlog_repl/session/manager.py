"""
Session Manager for the Sherlog REPL.
Handles creation, retrieval, and management of REPL sessions.
"""

import asyncio
import datetime
import logging
import uuid
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


class Session:
    """
    Represents a single REPL session with isolated namespace and state.
    """
    
    def __init__(self, session_id: str, user_context: Optional[Dict[str, Any]] = None):
        """
        Initialize a new session.
        
        Args:
            session_id: Unique identifier for the session.
            user_context: Optional user information and context.
        """
        self.id = session_id
        self.created_at = datetime.datetime.now()
        self.last_active = datetime.datetime.now()
        self.user_context = user_context or {}
        
        # Initialize the namespace with Python builtins
        self.namespace = {'__builtins__': __builtins__}
        
        # Keep track of registered tools
        self.registered_tools: Set[str] = set()
        
        # History of executed commands
        self.history: List[Dict[str, Any]] = []
        
        # Session lock for concurrency control
        self.lock = asyncio.Lock()
    
    def update_last_active(self):
        """Update the last active timestamp."""
        self.last_active = datetime.datetime.now()
    
    def add_to_history(self, command: str, result: Any):
        """
        Add a command and its result to the history.
        
        Args:
            command: The executed command.
            result: The result of the command.
        """
        self.history.append({
            'timestamp': datetime.datetime.now(),
            'command': command,
            'result': str(result)  # Convert to string for storage
        })
    
    def register_tool(self, tool_name: str):
        """
        Register a tool as available for this session.
        
        Args:
            tool_name: The name of the tool to register.
        """
        self.registered_tools.add(tool_name)
    
    def is_tool_registered(self, tool_name: str) -> bool:
        """
        Check if a tool is registered for this session.
        
        Args:
            tool_name: The name of the tool to check.
            
        Returns:
            True if the tool is registered, False otherwise.
        """
        return tool_name in self.registered_tools
    
    def clear(self):
        """Clear the session namespace, keeping only builtins."""
        builtins = self.namespace.get('__builtins__')
        self.namespace.clear()
        self.namespace['__builtins__'] = builtins
    
    def get_idle_time(self) -> float:
        """
        Get the number of seconds since this session was last active.
        
        Returns:
            Number of seconds since last activity.
        """
        now = datetime.datetime.now()
        return (now - self.last_active).total_seconds()
    
    def __repr__(self) -> str:
        return (f"Session(id={self.id}, "
                f"created={self.created_at}, "
                f"last_active={self.last_active}, "
                f"tools={len(self.registered_tools)})")


class SessionManager:
    """
    Manages multiple user sessions, providing creation, retrieval, and cleanup.
    """
    
    def __init__(self, max_sessions: int = 100, idle_timeout: int = 3600):
        """
        Initialize the session manager.
        
        Args:
            max_sessions: Maximum number of concurrent sessions.
            idle_timeout: Timeout in seconds for idle sessions.
        """
        self.sessions: Dict[str, Session] = {}
        self.max_sessions = max_sessions
        self.idle_timeout = idle_timeout
        self.cleanup_task = None
    
    async def create_session(self, session_id: Optional[str] = None, 
                            user_context: Optional[Dict[str, Any]] = None) -> Session:
        """
        Create a new session.
        
        Args:
            session_id: Optional session ID. If not provided, a UUID will be generated.
            user_context: Optional user information and context.
            
        Returns:
            The newly created session.
            
        Raises:
            RuntimeError: If the maximum number of sessions is reached.
        """
        if len(self.sessions) >= self.max_sessions:
            # Try to clean up idle sessions first
            await self.cleanup_idle_sessions()
            if len(self.sessions) >= self.max_sessions:
                raise RuntimeError(f"Maximum number of sessions ({self.max_sessions}) reached")
        
        session_id = session_id or str(uuid.uuid4())
        session = Session(session_id, user_context)
        self.sessions[session_id] = session
        logger.info(f"Created new session: {session_id}")
        return session
    
    async def get_session(self, session_id: str) -> Optional[Session]:
        """
        Get an existing session by ID.
        
        Args:
            session_id: The ID of the session to retrieve.
            
        Returns:
            The session if found, None otherwise.
        """
        session = self.sessions.get(session_id)
        if session:
            session.update_last_active()
        return session
    
    async def get_or_create_session(self, session_id: Optional[str] = None,
                                   user_context: Optional[Dict[str, Any]] = None) -> Session:
        """
        Get an existing session or create a new one.
        
        Args:
            session_id: The ID of the session to retrieve or create.
            user_context: Optional user information for new sessions.
            
        Returns:
            The retrieved or newly created session.
        """
        if session_id and session_id in self.sessions:
            session = await self.get_session(session_id)
            return session
        return await self.create_session(session_id, user_context)
    
    async def terminate_session(self, session_id: str) -> bool:
        """
        Terminate and clean up a session.
        
        Args:
            session_id: The ID of the session to terminate.
            
        Returns:
            True if the session was found and terminated, False otherwise.
        """
        if session_id in self.sessions:
            # Additional cleanup could happen here
            del self.sessions[session_id]
            logger.info(f"Terminated session: {session_id}")
            return True
        return False
    
    async def cleanup_idle_sessions(self) -> int:
        """
        Clean up sessions that have been idle for longer than the timeout.
        
        Returns:
            The number of sessions that were cleaned up.
        """
        now = datetime.datetime.now()
        idle_sessions = []
        
        for session_id, session in self.sessions.items():
            idle_time = session.get_idle_time()
            if idle_time > self.idle_timeout:
                idle_sessions.append(session_id)
        
        for session_id in idle_sessions:
            await self.terminate_session(session_id)
        
        if idle_sessions:
            logger.info(f"Cleaned up {len(idle_sessions)} idle sessions")
        
        return len(idle_sessions)
    
    async def start_cleanup_task(self):
        """Start a background task to periodically clean up idle sessions."""
        if self.cleanup_task and not self.cleanup_task.done():
            return
        
        async def cleanup_loop():
            while True:
                try:
                    await asyncio.sleep(60)  # Check every minute
                    await self.cleanup_idle_sessions()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Error in cleanup task: {e}")
        
        self.cleanup_task = asyncio.create_task(cleanup_loop())
    
    async def stop_cleanup_task(self):
        """Stop the background cleanup task."""
        if self.cleanup_task and not self.cleanup_task.done():
            self.cleanup_task.cancel()
            try:
                await self.cleanup_task
            except asyncio.CancelledError:
                pass
    
    def __repr__(self) -> str:
        return f"SessionManager(sessions={len(self.sessions)}/{self.max_sessions})"