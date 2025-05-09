"""
Concurrency utilities for the Sherlog REPL.
Provides tools for managing concurrent execution of tasks.
"""

import asyncio
import logging
import uuid
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


class ConcurrencyManager:
    """
    Manages concurrent execution of tasks with limits and tracking.
    """
    
    def __init__(self, max_concurrent_tasks: int = 10):
        """
        Initialize the concurrency manager.
        
        Args:
            max_concurrent_tasks: Maximum number of concurrent tasks to allow.
        """
        self.semaphore = asyncio.Semaphore(max_concurrent_tasks)
        self.active_tasks: Dict[str, asyncio.Task] = {}
        self.session_tasks: Dict[str, Set[str]] = {}
    
    async def run_task(self, session_id: str, 
                      coro_func: Callable[..., Any], 
                      *args: Any, 
                      **kwargs: Any) -> Any:
        """
        Run a coroutine as a task with concurrency control.
        
        Args:
            session_id: The ID of the session the task belongs to.
            coro_func: The coroutine function to run.
            *args: Positional arguments to pass to the coroutine.
            **kwargs: Keyword arguments to pass to the coroutine.
            
        Returns:
            The result of the coroutine.
        """
        task_id = f"{session_id}_{uuid.uuid4()}"
        
        # Track tasks by session
        if session_id not in self.session_tasks:
            self.session_tasks[session_id] = set()
        
        async with self.semaphore:
            try:
                # Create and track the task
                task = asyncio.create_task(coro_func(*args, **kwargs))
                self.active_tasks[task_id] = task
                self.session_tasks[session_id].add(task_id)
                
                # Wait for the task to complete
                result = await task
                return result
            finally:
                # Clean up task tracking
                if task_id in self.active_tasks:
                    del self.active_tasks[task_id]
                if session_id in self.session_tasks:
                    self.session_tasks[session_id].discard(task_id)
                    # Clean up empty session entries
                    if not self.session_tasks[session_id]:
                        del self.session_tasks[session_id]
    
    async def cancel_session_tasks(self, session_id: str) -> List[Tuple[str, bool]]:
        """
        Cancel all tasks for a session.
        
        Args:
            session_id: The ID of the session whose tasks should be cancelled.
            
        Returns:
            A list of (task_id, success) tuples indicating which tasks were
            successfully cancelled.
        """
        if session_id not in self.session_tasks:
            return []
        
        task_ids = list(self.session_tasks[session_id])
        results = []
        
        for task_id in task_ids:
            if task_id in self.active_tasks:
                task = self.active_tasks[task_id]
                if not task.done():
                    task.cancel()
                    try:
                        await task
                        success = True
                    except asyncio.CancelledError:
                        success = True
                    except Exception as e:
                        logger.error(f"Error cancelling task {task_id}: {e}")
                        success = False
                    
                    results.append((task_id, success))
                    del self.active_tasks[task_id]
        
        # Clear the session's task set
        self.session_tasks[session_id].clear()
        del self.session_tasks[session_id]
        
        return results
    
    async def cancel_all_tasks(self) -> int:
        """
        Cancel all active tasks.
        
        Returns:
            The number of tasks that were cancelled.
        """
        task_ids = list(self.active_tasks.keys())
        count = 0
        
        for task_id in task_ids:
            task = self.active_tasks[task_id]
            if not task.done():
                task.cancel()
                try:
                    await task
                    count += 1
                except (asyncio.CancelledError, Exception):
                    count += 1
        
        # Clear all tracking data
        self.active_tasks.clear()
        self.session_tasks.clear()
        
        return count
    
    def get_active_task_count(self, session_id: Optional[str] = None) -> int:
        """
        Get the count of active tasks.
        
        Args:
            session_id: Optional session ID to count tasks for a specific session.
            
        Returns:
            The number of active tasks.
        """
        if session_id:
            return len(self.session_tasks.get(session_id, set()))
        return len(self.active_tasks)