"""
Sherlog-Canvas Unified REPL MCP Server.
Provides a persistent Python REPL with access to multiple MCP tools.
"""

import asyncio
import io
import json
import logging
import os
import sys
import time
import traceback
from contextlib import redirect_stdout, redirect_stderr, asynccontextmanager
from typing import Any, Dict, List, Optional, Set, Union, cast
import re
import subprocess
from collections.abc import AsyncIterator
import inspect

from mcp.server.fastmcp import FastMCP, Context
import mcp.types as types

from .session import SessionManager, Session
from .proxy import ToolProxyGenerator
from .connectors import GitHubConnector, FilesystemConnector, JiraConnector
from .utils import ConcurrencyManager

# Configure logging
LOG_LEVEL = os.environ.get("SHERLOG_LOG_LEVEL", "INFO").upper()
LOG_FILE = os.environ.get("SHERLOG_LOG_FILE")

# Configure handlers based on environment
handlers: List[logging.Handler] = [logging.StreamHandler()]
if LOG_FILE:
    handlers.append(logging.FileHandler(LOG_FILE))

# Set up root logger
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=handlers
)
logger = logging.getLogger("sherlog_repl")

# ---------------------------------------------------------------------------
# Utility: ensure nest_asyncio is loaded & applied so that synchronous wrappers
#          can call async MCP proxies inside a running event loop.
# ---------------------------------------------------------------------------


_NEST_ASYNCIO_READY = False


def _ensure_nest_asyncio() -> None:
    """Import and apply ``nest_asyncio``. If missing, attempt on-the-fly install."""
    global _NEST_ASYNCIO_READY
    if _NEST_ASYNCIO_READY:
        return

    try:
        import nest_asyncio  # type: ignore
        nest_asyncio.apply()
        _NEST_ASYNCIO_READY = True
        logger.debug("nest_asyncio applied successfully")
    except ModuleNotFoundError:
        logger.info("nest_asyncio not present – attempting runtime install via uv …")
        try:
            # Best-effort install, suppress output
            subprocess.run(["uv", "pip", "install", "nest_asyncio", "--quiet"], check=True)
            import importlib  # noqa: WPS433 – runtime import OK here
            nest_asyncio = importlib.import_module("nest_asyncio")  # type: ignore
            nest_asyncio.apply()
            _NEST_ASYNCIO_READY = True
            logger.info("nest_asyncio installed & applied at runtime")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to auto-install nest_asyncio – nested event-loop calls may error. %s",
                exc,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("nest_asyncio import failed: %s", exc)

# ---- FastMCP instance and lifespan --------------------------------------


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[None]:
    """Run global startup and shutdown within FastMCP lifespan."""
    await startup()
    try:
        yield
    finally:
        await shutdown()

# Initialize MCP server early so decorators below can reference it
mcp = FastMCP(
    "sherlog-repl",
    log_level=LOG_LEVEL,
    lifespan=app_lifespan,
)

# -------------------------------------------------------------------------

# Global state
session_manager = SessionManager()
tool_proxy_generator = ToolProxyGenerator()
concurrency_manager = ConcurrencyManager()

# Variable to store connector instances
connectors = {}


async def set_working_dir_from_roots(ctx: Context, session_id: str) -> None:
    """Get roots from client and set working directory to first root if available."""
    # Best effort to set working directory. Errors are logged but not raised.
    # This is because os.chdir is a global operation and might affect other concurrent sessions.
    # For true CWD isolation per session, the execution environment itself (e.g. Docker per session)
    # would need to be more complex. For now, this is a shared CWD for the server process.
    try:
        # This uses list_roots() from the MCP session object linked to the context.
        # The actual ctx.session object here is the FastMCP session, not sherlog_repl.Session
        # We need to ensure this ctx.session is what we expect.
        # Assuming ctx.session provided by FastMCP has list_roots capability.
        if hasattr(ctx, 'session') and hasattr(ctx.session, 'list_roots'):
            roots_result: Optional[types.ListRootsResult] = await ctx.session.list_roots()

            if roots_result and hasattr(roots_result, 'roots') and roots_result.roots:
                root = roots_result.roots[0]
                uri_str = str(root.uri)
                if uri_str.startswith("file://"):
                    path = uri_str.replace("file://", "")
                    path = os.path.normpath(path)
                    current_dir = os.path.normpath(os.getcwd())
                    if path != current_dir:
                        try:
                            logger.info(f"[{getattr(ctx, 'request_id', 'unknown')}] Attempting to change CWD to {path} for session {session_id}")
                            os.chdir(path)
                            logger.info(f"[{getattr(ctx, 'request_id', 'unknown')}] Changed CWD to {path} for session {session_id}")
                        except OSError as e:
                            logger.warning(f"[{getattr(ctx, 'request_id', 'unknown')}] Failed to change CWD to {path} for session {session_id}: {e}")
                    else:
                        logger.info(f"[{getattr(ctx, 'request_id', 'unknown')}] Requested CWD {path} is already current for session {session_id}.")
            else:
                logger.info(f"[{getattr(ctx, 'request_id', 'unknown')}] No roots found or roots list empty for session {session_id}.")
        else:
            logger.info(f"[{getattr(ctx, 'request_id', 'unknown')}] ctx.session.list_roots not available for session {session_id}.")

    except Exception as e:
        logger.error(f"[{getattr(ctx, 'request_id', 'unknown')}] Error in set_working_dir_from_roots for session {session_id}: {str(e)}")
        logger.info(f"[{getattr(ctx, 'request_id', 'unknown')}] Traceback: {traceback.format_exc()}")


async def initialize_connectors(ctx: Context) -> bool:
    """
    Initialize all MCP connectors.
    
    Args:
        ctx: The context for the MCP call.
        
    Returns:
        True if at least one connector was initialized successfully, False otherwise.
    """
    global connectors
    
    # Prevent multiple initializations
    if connectors:
        logger.info("Connectors already initialized, skipping initialization")
        return True
    
    logger.info("Starting connector initialization")
    
    # Create connector instances with configuration
    github_config = {
        "github_token": os.environ.get("GITHUB_TOKEN"),
        "container_image": os.environ.get("GITHUB_MCP_IMAGE", "ghcr.io/github/github-mcp-server")
    }
    
    filesystem_config = {
        "root_dirs": [os.getcwd()],
        "container_image": os.environ.get("FILESYSTEM_MCP_IMAGE", "modelcontextprotocol/filesystem-mcp-server:latest")
    }
    
    jira_config = {
        "jira_url": os.environ.get("JIRA_URL"),
        "jira_token": os.environ.get("JIRA_API_TOKEN"),
        "jira_email": os.environ.get("JIRA_EMAIL"),
        "container_image": os.environ.get("JIRA_MCP_IMAGE", "sooperset/mcp-atlassian:latest")
    }
    
    # Log configuration (but hide sensitive tokens)
    safe_github_config = github_config.copy()
    if safe_github_config.get("github_token"):
        safe_github_config["github_token"] = "****"
        
    safe_jira_config = jira_config.copy()
    for key in ["jira_token", "jira_email"]:
        if safe_jira_config.get(key):
            safe_jira_config[key] = "****"
            
    logger.info(f"GitHub connector config: {safe_github_config}")
    logger.info(f"Filesystem connector config: {filesystem_config}")
    logger.info(f"Jira connector config: {safe_jira_config}")
    
    # Initialize connectors
    connectors = {
        "github": GitHubConnector(github_config),
        "filesystem": FilesystemConnector(filesystem_config),
        "jira": JiraConnector(jira_config)
    }
    
    # Register connectors with the tool proxy generator
    for connector in connectors.values():
        tool_proxy_generator.register_connector(connector)
        logger.info(f"Registered {connector.name} connector with tool proxy generator")
    
    # Initialize connectors in parallel
    logger.info("Starting parallel connector initialization")
    start_time = time.time()
    initialization_results = await asyncio.gather(
        *[connector.initialize() for connector in connectors.values()],
        return_exceptions=True
    )
    end_time = time.time()
    logger.info(f"Connector initialization completed in {end_time - start_time:.2f} seconds")
    
    # Check for exceptions and success
    success_count = 0
    for i, result in enumerate(initialization_results):
        connector_name = list(connectors.keys())[i]
        if isinstance(result, Exception):
            logger.error(f"Failed to initialize {connector_name} connector: {result}")
        elif not result:
            logger.warning(f"Failed to initialize {connector_name} connector")
        else:
            logger.info(f"Successfully initialized {connector_name} connector")
            success_count += 1
    
    # Generate proxy functions for all tools
    if success_count > 0:
        try:
            logger.info("Generating tool proxies")
            proxy_start = time.time()
            proxies = await tool_proxy_generator.generate_all_proxies()
            proxy_end = time.time()
            logger.info(f"Generated {len(proxies)} tool proxies in {proxy_end - proxy_start:.2f} seconds")
        except Exception as e:
            logger.error(f"Error generating tool proxies: {e}")
            logger.info(f"Tool proxy error details: {traceback.format_exc()}")
    
    # Return success if at least one connector was initialized
    return success_count > 0


async def get_or_create_session(ctx: Context) -> Session:
    """
    Get or create a session for the current context.
    
    Args:
        ctx: The context for the MCP call.
        
    Returns:
        A Session object.
    """
    # Use notebook_id as session_id if available
    session_id = None
    if hasattr(ctx, 'request_metadata') and ctx.request_metadata:
        session_id = ctx.request_metadata.get('notebook_id')
    
    # If session_id is still None, use a default value
    session_id = session_id or "default"
    
    # Get user information from the context
    user_context = {}
    if hasattr(ctx, 'user_identity') and ctx.user_identity:
        user_context['user'] = ctx.user_identity
        logger.info(f"User context for session {session_id}: {user_context}")
    
    # Get or create the session
    start_time = time.time()
    session = await session_manager.get_or_create_session(session_id, user_context)
    end_time = time.time()
    
    if end_time - start_time > 0.1:  # Log only if it took more than 100ms
        logger.info(f"Get or create session {session_id} took {end_time - start_time:.2f} seconds")
    
    return session


@mcp.tool()
async def execute_python(ctx: Context, code: str, reset: bool = False, register_tools: Optional[List[str]] = None) -> List[types.TextContent]:
    """
    Execute Python code with access to MCP tools.
    
    Args:
        ctx: The context for the MCP call.
        code: The Python code to execute.
        reset: Whether to reset the session (clear all variables).
        register_tools: List of tool domains to register (e.g., ["github", "filesystem"]).
        
    Returns:
        The result of the execution.
    """
    request_id = getattr(ctx, 'request_id', 'unknown')
    logger.info(f"[{request_id}] execute_python called: reset={reset}, register_tools={register_tools}")
    logger.info(f"[{request_id}] Code to execute: {code[:200]}{'...' if len(code) > 200 else ''}")
    
    start_time = time.time()
    
    # Get or create the Sherlog session (different from ctx.session potentially)
    sherlog_session = await get_or_create_session(ctx)
    logger.info(f"[{request_id}] Using sherlog session: {sherlog_session.id} (age: {time.time() - sherlog_session.created_at.timestamp():.1f}s)")

    # Attempt to set working directory based on client roots
    # Pass the sherlog_session.id for logging clarity
    await set_working_dir_from_roots(ctx, sherlog_session.id)

    # Initialize connectors if needed (this also generates all proxies)
    if not connectors:
        logger.info(f"[{request_id}] Initializing connectors for first time")
        success = await initialize_connectors(ctx)
        if not success:
            logger.warning(f"[{request_id}] Not all connectors initialized successfully")
            return [types.TextContent(type="text", text="WARNING: Not all MCP connectors could be initialized. Some tools may not be available.")]
    
    # Handle reset
    if reset:
        logger.info(f"[{request_id}] Resetting session {sherlog_session.id}")
        sherlog_session.clear()
        return [types.TextContent(type="text", text="Python session reset. All variables cleared.")]
    
    # Register tools if requested
    if register_tools:
        logger.info(f"[{request_id}] Registering tools: {register_tools}")
        for domain in register_tools:
            if domain not in connectors: # Check against globally initialized connectors
                logger.warning(f"[{request_id}] Unknown domain requested for registration: {domain}")
                # Consider returning an error or specific message here
                # return [types.TextContent(type="text", text=f"Unknown domain for registration: {domain}")]
                continue # Skip this domain
            
            # Check if connector is initialized
            # This assumes `connectors` global dict stores the initialized connector instances
            if domain in connectors and not connectors[domain].initialized:
                logger.info(f"[{request_id}] Connector {domain} not initialized, attempting initialization")
                # Initialize the specific connector if not already done
                # The initialize() method of connectors is idempotent
                init_success = await connectors[domain].initialize() 
                if not init_success:
                    logger.warning(f"[{request_id}] Failed to initialize connector during tool registration: {domain}")
                    # return [types.TextContent(type="text", text=f"Cannot register tools from {domain}. Connector could not be initialized.")]
                    continue # Skip this domain

            # Register all tools from the domain into the current sherlog_session.namespace
            # ToolProxyGenerator should have been populated by initialize_connectors if it ran.
            # If initialize_connectors hasn't run, tool_proxy_generator might be empty for this domain.
            domain_start = time.time()
            # Ensure tools for this domain are discovered if not already by a global call
            await tool_proxy_generator.discover_tools(domain) # Ensures proxies for this domain are known
            count = tool_proxy_generator.register_proxies_in_namespace(sherlog_session.namespace, connector_name=domain)
            domain_end = time.time()
            logger.info(f"[{request_id}] Registered {count} tools from {domain} in session {sherlog_session.id} ({domain_end - domain_start:.2f}s)")

    # Auto-register tools for all initialized connectors on first use to make them
    # immediately available to user/LLM without needing an explicit register_tools call.
    if not register_tools:
        # Use a sentinel in the session namespace so we only do this once per session.
        if not sherlog_session.namespace.get("_auto_tools_registered", False):
            auto_reg_start = time.time()
            registered_auto_total = 0
            auto_registered_tool_names: List[str] = []
            for domain_name, connector in connectors.items():
                if not connector.initialized:
                    continue  # Skip connectors that failed to init or were not configured

                # Ensure we have discovered the tools for this domain
                try:
                    await tool_proxy_generator.discover_tools(domain_name)
                except Exception as e:
                    logger.warning(
                        f"[{request_id}] Failed discovering tools for domain {domain_name} during auto-registration: {e}")
                    continue

                try:
                    # Register proxies and track names
                    # Snapshot keys before registration to know which were added
                    before_keys = set(sherlog_session.namespace.keys())
                    count_auto = tool_proxy_generator.register_proxies_in_namespace(
                        sherlog_session.namespace, connector_name=domain_name)
                    registered_auto_total += count_auto
                    added_keys = [k for k in sherlog_session.namespace.keys() if k not in before_keys]
                    auto_registered_tool_names.extend(sorted(added_keys))
                    logger.info(
                        f"[{request_id}] Auto-registered {count_auto} tools from {domain_name} in session {sherlog_session.id}")
                except Exception as e:
                    logger.warning(
                        f"[{request_id}] Failed auto-registering tools from {domain_name}: {e}")

            sherlog_session.namespace["_auto_tools_registered"] = True
            # Keep the list for later use in the response text
            sherlog_session.namespace["_auto_tools_registered_list"] = auto_registered_tool_names
            auto_reg_end = time.time()
            logger.info(
                f"[{request_id}] Automatic tool registration complete: {registered_auto_total} tools (took {auto_reg_end - auto_reg_start:.2f}s)")

    # Record execution start now that setup is complete
    execution_start = time.time()

    # Patch event loop to allow nested asyncio.run inside user code (e.g., when calling async MCP proxies)
    try:
        _ensure_nest_asyncio()
    except Exception as e:
        logger.warning(f"Failed to apply nest_asyncio patch: {e}")

    # Step 1: inject context variables while holding the session lock to
    # avoid concurrent mutation of the namespace
    async with sherlog_session.lock:
        sherlog_session.namespace["ctx"] = ctx
        sherlog_session.namespace["_ctx"] = ctx

    # Step 2: define a synchronous helper that actually performs the exec
    def _sync_exec(_code: str, _ns: Dict[str, Any]) -> tuple[str, str]:  # noqa: WPS430
        _stdout = io.StringIO()
        _stderr = io.StringIO()
        with redirect_stdout(_stdout), redirect_stderr(_stderr):
            exec(_code, _ns)
        return _stdout.getvalue(), _stderr.getvalue()

    # Wrap in to_thread so the event-loop remains available
    async def _exec_async() -> tuple[str, str]:  # noqa: WPS430
        return await asyncio.to_thread(_sync_exec, code, sherlog_session.namespace)

    try:
        logger.info(f"[{request_id}] Scheduling user code for execution (session {sherlog_session.id})")
        output, errors = await concurrency_manager.run_task(
            sherlog_session.id,
            _exec_async,
        )
        
        # Record in session history
        result_text = output or errors or "Code executed successfully (no output)"
        sherlog_session.add_to_history(code, result_text)
        
        # Prepare the response
        result = ""
        # If we performed automatic registration earlier in this call, surface the tool names first
        auto_tools_msg = ""
        auto_tools_names: List[str] = sherlog_session.namespace.pop("_auto_tools_registered_list", []) if "_auto_tools_registered_list" in sherlog_session.namespace else []
        if auto_tools_names:
            auto_tools_msg = (
                "Automatically registered MCP tools now available in this session:\n"
                + ", ".join(auto_tools_names[:50]) + (" ..." if len(auto_tools_names) > 50 else "")
                + "\n\n"
            )

        result += auto_tools_msg
        if output:
            result += f"Output:\n{output}"
            logger.info(f"[{request_id}] Code produced output: {len(output)} chars")
        if errors:
            result += f"\nErrors:\n{errors}"
            logger.warning(f"[{request_id}] Code produced errors: {errors}")
        if not output and not errors:
            try:
                # Try to evaluate the last line for expression results
                last_line = code.strip().split('\n')[-1]
                last_value = eval(last_line, sherlog_session.namespace)
                result = f"Result: {repr(last_value)}"
                logger.info(f"[{request_id}] Evaluated expression result: {repr(last_value)[:100]}{'...' if len(repr(last_value)) > 100 else ''}")
            except (SyntaxError, ValueError, NameError):
                result = "Code executed successfully (no output)"
                logger.info(f"[{request_id}] Code executed with no output")
        
        execution_end = time.time()
        total_time = execution_end - start_time
        execution_time = execution_end - execution_start
        logger.info(f"[{request_id}] execute_python completed in {total_time:.2f}s (execution: {execution_time:.2f}s)")
        
        return [types.TextContent(type="text", text=result)]
                
    except Exception as e:
        error_msg = f"Error executing code:\n{traceback.format_exc()}"
        logger.error(f"[{request_id}] Failed to execute code: {str(e)}")
        logger.info(f"[{request_id}] Error details: {traceback.format_exc()}")
        return [types.TextContent(type="text", text=error_msg)]


@mcp.tool()
async def list_available_tools(ctx: Context, domain: Optional[str] = None) -> List[types.TextContent]:
    """
    List available tools from MCP connectors.
    
    Args:
        ctx: The context for the MCP call.
        domain: Optional domain to filter tools by (e.g., "github", "filesystem").
        
    Returns:
        A list of available tools.
    """
    request_id = getattr(ctx, 'request_id', 'unknown')
    logger.info(f"[{request_id}] list_available_tools called: domain={domain}")
    
    start_time = time.time()
    
    # Initialize connectors if needed
    if not connectors:
        logger.info(f"[{request_id}] Initializing connectors for first time")
        success = await initialize_connectors(ctx)
        if not success:
            logger.warning(f"[{request_id}] Not all connectors initialized successfully")
            return [types.TextContent(type="text", text="WARNING: Not all MCP connectors could be initialized. Some tools may not be available.")]
    
    # Get all tools from the proxy generator
    try:
        tools = []
        if domain:
            logger.info(f"[{request_id}] Listing tools for specific domain: {domain}")
            if domain not in connectors:
                logger.warning(f"[{request_id}] Unknown domain requested: {domain}")
                return [types.TextContent(type="text", text=f"Unknown domain: {domain}")]
            
            # Check if connector is initialized
            if not connectors[domain].initialized:
                logger.warning(f"[{request_id}] Connector not initialized: {domain}")
                return [types.TextContent(type="text", text=f"The {domain} connector is not initialized. Please check your environment variables or configuration.")]
            
            tools = await tool_proxy_generator.discover_tools(domain)
            logger.info(f"[{request_id}] Found {len(tools)} tools for domain {domain}")
        else:
            logger.info(f"[{request_id}] Listing tools for all domains")
            tools = await tool_proxy_generator.discover_tools()
            logger.info(f"[{request_id}] Found {len(tools)} tools across all domains")
        
        # Format the tool information
        if not tools:
            logger.warning(f"[{request_id}] No tools found")
            return [types.TextContent(type="text", text="No tools available. Please check that at least one connector is properly configured.")]
        
        # Group tools by domain
        tools_by_domain = {}
        for tool in tools:
            domain = tool.get('connector')
            if domain not in tools_by_domain:
                tools_by_domain[domain] = []
            tools_by_domain[domain].append(tool)
        
        # Format the result
        result = "Available MCP Tools:\n\n"
        for domain, domain_tools in tools_by_domain.items():
            domain_name = domain if domain is not None else "General"
            result += f"## {domain_name.capitalize()} Tools\n\n"
            for tool in domain_tools:
                tool_id = tool.get('tool_id')
                description = tool.get('description', 'No description')
                result += f"- **{domain}_{tool_id}**: {description}\n"
            result += "\n"
        
        # Add status of unavailable connectors
        unavailable = []
        for name, connector in connectors.items():
            if not connector.initialized:
                unavailable.append(name)
                logger.info(f"[{request_id}] Connector not available: {name}")
        
        if unavailable:
            result += f"\n## Unavailable Connectors\n\n"
            for name in unavailable:
                if name == "github":
                    result += "- **GitHub**: To enable GitHub tools, set the GITHUB_TOKEN environment variable.\n"
                elif name == "jira":
                    result += "- **Jira**: To enable Jira tools, set the JIRA_URL, JIRA_API_TOKEN, and JIRA_EMAIL environment variables.\n"
                else:
                    result += f"- **{name.capitalize()}**: Not configured properly.\n"
        
        end_time = time.time()
        logger.info(f"[{request_id}] list_available_tools completed in {end_time - start_time:.2f}s")
        
        return [types.TextContent(type="text", text=result)]
    
    except Exception as e:
        error_msg = f"Error listing tools: {str(e)}"
        logger.error(f"[{request_id}] Failed to list tools: {str(e)}")
        logger.info(f"[{request_id}] Error details: {traceback.format_exc()}")
        return [types.TextContent(type="text", text=error_msg)]


@mcp.tool()
async def register_tools(ctx: Context, domains: List[str]) -> List[types.TextContent]:
    """
    Register tools from specified domains in the current Sherlog session.
    This makes the tools available as callable functions in the `execute_python` context for that session.
    
    Args:
        ctx: The context for the MCP call.
        domains: List of domains to register tools from (e.g., ["github", "filesystem"]).
        
    Returns:
        A confirmation message.
    """
    request_id = getattr(ctx, 'request_id', 'unknown')
    logger.info(f"[{request_id}] register_tools (for session) called: domains={domains}")
    
    start_time = time.time()
    
    # Initialize connectors if needed (this also generates all proxies)
    # This ensures that tool_proxy_generator has a chance to discover tools from all connectors globally
    if not connectors: # Global check
        logger.info(f"[{request_id}] Initializing all connectors for first time (called from register_tools)")
        # Use a dummy context if ctx is not suitable for global initialization
        # request_id is from the outer scope of register_tools (defined L476)
        class TempCtx:
            def __init__(self, req_id_val: str):
                self.request_id: str = f"init_from_reg_{req_id_val}"
                self.request_metadata: Dict[str, Any] = {}
                self.user_identity: Dict[str, Any] = {}
        
        init_success = await initialize_connectors(cast(Context, TempCtx(req_id_val=request_id)))
        if not init_success:
            logger.warning(f"[{request_id}] Not all connectors initialized successfully during mass initialization.")
            # Depending on strictness, might return a warning or error here
            # return [types.TextContent(type="text", text="WARNING: Not all MCP connectors could be initialized globally. Some domains may not be available.")]

    # Get or create the Sherlog session
    sherlog_session = await get_or_create_session(ctx)
    logger.info(f"[{request_id}] Using sherlog session: {sherlog_session.id}")
    
    # Validate domain names against globally known connectors
    invalid_domains = [d for d in domains if d not in connectors]
    if invalid_domains:
        logger.warning(f"[{request_id}] Unknown domains requested for registration: {invalid_domains}")
        return [types.TextContent(type="text", text=f"Unknown domains: {', '.join(invalid_domains)}. Available: {list(connectors.keys())}")]
    
    registered_counts = {}
    unavailable_due_to_init_failure = []
    
    try:
        async with sherlog_session.lock: # Lock the specific Sherlog session
            for domain in domains:
                logger.info(f"[{request_id}] Attempting to register tools from domain: {domain} into session {sherlog_session.id}")
                
                # Check if the requested connector itself is initialized (e.g. Docker container running)
                connector_instance = connectors.get(domain)
                if not connector_instance or not connector_instance.initialized:
                    logger.warning(f"[{request_id}] Connector for domain '{domain}' is not initialized or does not exist. Attempting to initialize it now.")
                    if connector_instance:
                        # One last attempt to initialize the specific connector
                        if not await connector_instance.initialize():
                            logger.error(f"[{request_id}] Failed to initialize connector '{domain}' on demand for session registration.")
                            unavailable_due_to_init_failure.append(domain)
                            continue # Skip to next domain
                    else: # Should not happen if invalid_domains check above is working
                         logger.error(f"[{request_id}] Connector for domain '{domain}' unexpectedly not found in global connectors dict.")
                         unavailable_due_to_init_failure.append(domain)
                         continue


                # Proxies for this domain should have been generated by initialize_connectors
                # or by the initial discovery in tool_proxy_generator.
                # We ensure the tool_proxy_generator is aware of tools for this domain
                # and then register them into the specific session's namespace.
                domain_reg_start = time.time()
                await tool_proxy_generator.discover_tools(domain) # Ensures tool_info for the domain is populated in the generator
                
                # Register available proxies for this domain into the current session's namespace
                count = tool_proxy_generator.register_proxies_in_namespace(sherlog_session.namespace, connector_name=domain)
                
                domain_reg_end = time.time()
                registered_counts[domain] = count
                logger.info(f"[{request_id}] Registered {count} tools from {domain} in session {sherlog_session.id} namespace ({domain_reg_end - domain_reg_start:.2f}s)")
        
        # Format the result
        result_parts = []
        if registered_counts:
            result_parts.append("Tools registered in current session's Python execution namespace:")
            for domain, count in registered_counts.items():
                result_parts.append(f"- {domain}: {count} tools")

            # Provide concrete callable names and their signatures to help the LLM
            for domain in domains:
                funcs = [name for name in sherlog_session.namespace.keys() if name.startswith(f"{domain}_")]
                if funcs:
                    result_parts.append(f"\n### {domain.capitalize()} wrapper functions & signatures")
                    for fname in sorted(funcs):
                        func_obj = sherlog_session.namespace[fname]
                        try:
                            sig = str(inspect.signature(func_obj))
                        except (ValueError, TypeError):
                            sig = "(…)"
                        result_parts.append(f"- {fname}{sig}")
        else:
            result_parts.append("No new tools were registered in the session namespace.")

        if unavailable_due_to_init_failure:
            result_parts.append("\\nConnectors that failed to initialize for this registration:")
            for domain in unavailable_due_to_init_failure:
                result_parts.append(f"- {domain}")
        
        end_time = time.time()
        logger.info(f"[{request_id}] register_tools (for session) completed in {end_time - start_time:.2f}s")
        
        return [types.TextContent(type="text", text="\\n".join(result_parts))]
    
    except Exception as e:
        error_msg = f"Error registering tools in session: {str(e)}"
        logger.error(f"[{request_id}] Failed to register tools in session: {str(e)}")
        logger.info(f"[{request_id}] Error details: {traceback.format_exc()}")
        return [types.TextContent(type="text", text=error_msg)]


@mcp.tool()
async def install_package(ctx: Context, package_name: str) -> List[types.TextContent]:
    """
    Install a Python package using 'uv pip install'. 
    The package will be installed in the server's environment and an attempt will be made to import it.
    You may need to explicitly import the package in your script after installation.
    """
    request_id = getattr(ctx, 'request_id', 'unknown')
    logger.info(f"[{request_id}] install_package called: package_name={package_name}")

    # Get or create the Sherlog session to associate logs, though installation is global
    sherlog_session = await get_or_create_session(ctx)
    await set_working_dir_from_roots(ctx, sherlog_session.id) # Set CWD based on client roots

    # Basic validation for package name
    if not re.match(r"^[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,-]+\])?(?:[~<>=!]=?[A-Za-z0-9_.-]+)?$", package_name):
        logger.warning(f"[{request_id}] Invalid package name format: {package_name}")
        return [types.TextContent(type="text", text=f"Invalid package name format: {package_name}")]
    
    try:
        logger.info(f"[{request_id}] Attempting to install package: {package_name} using uv.")
        # Using sys.executable to ensure we use the correct Python environment's uv
        # if uv is installed as a script for that env.
        # Alternatively, ensure 'uv' is in PATH and refers to the correct one.
        process = await asyncio.create_subprocess_exec(
            "uv", "pip", "install", package_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        
        stdout_str = stdout.decode(errors='ignore').strip()
        stderr_str = stderr.decode(errors='ignore').strip()

        log_output = ""
        if stdout_str:
            log_output += f"UV STDOUT:\\n{stdout_str}\\n"
        if stderr_str:
            # uv often puts progress and non-error info to stderr
            log_output += f"UV STDERR:\\n{stderr_str}\\n"
        
        logger.info(f"[{request_id}] uv pip install {package_name} process completed. RC: {process.returncode}.\\n{log_output}")

        if process.returncode != 0:
            logger.error(f"[{request_id}] Failed to install package {package_name}. Error: {stderr_str or stdout_str}")
            return [types.TextContent(type="text", text=f"Failed to install package: {package_name}\\n{stderr_str or stdout_str}")]
        
        # Package installed. Attempt to import the base package name to confirm.
        # E.g., for "requests[security]" or "numpy==1.2.3", try to import "requests" or "numpy"
        base_package_name = package_name.split('[')[0].split('==')[0].split('~=')[0].split('>=')[0].split('<=')[0].split('<')[0].split('>')[0].split('!=')[0]
        base_package_name = re.sub(r"[^A-Za-z0-9_].*", "", base_package_name) # Clean up further

        if not base_package_name:
             logger.warning(f"[{request_id}] Could not determine base package name from {package_name} for import check.")
             return [types.TextContent(type="text", text=f"Successfully initiated installation for {package_name}. Please try importing it.")]

        try:
            # We can't easily import into the exec session's namespace from here.
            # This import check happens in the server's global scope.
            # The user will need to import it in their `execute_python` call.
            exec(f"import {base_package_name}", {}) # Test import in a dummy namespace
            logger.info(f"[{request_id}] Successfully installed {package_name}. Test import of '{base_package_name}' succeeded in server global scope.")
            return [types.TextContent(type="text", text=f"Successfully installed {package_name}. Please import '{base_package_name}' in your script.")]
        except ImportError as e:
            logger.warning(f"[{request_id}] Package {package_name} installed, but test import of '{base_package_name}' failed in server global scope: {e}")
            return [types.TextContent(type="text", text=f"Package {package_name} installed, but an import test failed: {e}. You might still be able to import it.")]
            
    except FileNotFoundError:
        logger.error(f"[{request_id}] 'uv' command not found. Please ensure 'uv' is installed and in the PATH.")
        return [types.TextContent(type="text", text="'uv' command not found. Cannot install packages.")]
    except Exception as e:
        error_msg = f"Error installing package {package_name}: {str(e)}\\n{traceback.format_exc()}"
        logger.error(f"[{request_id}] {error_msg}")
        return [types.TextContent(type="text", text=error_msg)]


@mcp.tool()
async def list_session_variables(ctx: Context) -> List[types.TextContent]:
    """
    List all variables in the current session.
    
    Args:
        ctx: The context for the MCP call.
        
    Returns:
        A list of variables and their values.
    """
    request_id = getattr(ctx, 'request_id', 'unknown')
    logger.info(f"[{request_id}] list_session_variables called")
    
    start_time = time.time()
    
    # Get or create the session
    session = await get_or_create_session(ctx)
    logger.info(f"[{request_id}] Using session: {session.id} (age: {time.time() - session.created_at.timestamp():.1f}s)")
    
    # Get all variables
    vars_dict = {
        k: repr(v) for k, v in session.namespace.items() 
        if not k.startswith('_') and k != '__builtins__'
    }
    
    if not vars_dict:
        logger.info(f"[{request_id}] No variables found in session {session.id}")
        return [types.TextContent(type="text", text="No variables in current session.")]
    
    var_list = "\n".join(f"{k} = {v}" for k, v in vars_dict.items())
    logger.info(f"[{request_id}] Found {len(vars_dict)} variables in session {session.id}")
    
    end_time = time.time()
    logger.info(f"[{request_id}] list_session_variables completed in {end_time - start_time:.2f}s")
    
    return [types.TextContent(type="text", text=f"Current session variables:\n\n{var_list}")]


@mcp.tool()
async def reset_session(ctx: Context) -> List[types.TextContent]:
    """
    Reset the current session, clearing all variables.
    
    Args:
        ctx: The context for the MCP call.
        
    Returns:
        A confirmation message.
    """
    request_id = getattr(ctx, 'request_id', 'unknown')
    logger.info(f"[{request_id}] reset_session called")
    
    start_time = time.time()
    
    # Get or create the session
    session = await get_or_create_session(ctx)
    logger.info(f"[{request_id}] Using session: {session.id} (age: {time.time() - session.created_at.timestamp():.1f}s)")
    
    # Clear the session
    session.clear()
    logger.info(f"[{request_id}] Session {session.id} cleared")
    
    end_time = time.time()
    logger.info(f"[{request_id}] reset_session completed in {end_time - start_time:.2f}s")
    
    return [types.TextContent(type="text", text="Session reset. All variables cleared.")]


# Define startup function without decorator
async def startup():
    """
    Startup handler for the MCP server.
    Initialize the session manager and start background tasks.
    """
    logger.info("Sherlog-REPL server starting up")

    # Set session timeout from environment variable if provided
    if 'SHERLOG_SESSION_TIMEOUT' in os.environ:
        try:
            timeout = int(os.environ['SHERLOG_SESSION_TIMEOUT'])
            session_manager.idle_timeout = timeout
            logger.info(f"Session timeout set to {timeout} seconds")
        except ValueError:
            logger.warning(f"Invalid SHERLOG_SESSION_TIMEOUT value: {os.environ['SHERLOG_SESSION_TIMEOUT']}")
    
    # Log environment configuration
    env_vars = {
        "SHERLOG_LOG_LEVEL": os.environ.get("SHERLOG_LOG_LEVEL", "INFO"),
        "SHERLOG_LOG_FILE": os.environ.get("SHERLOG_LOG_FILE", "None"),
        "SHERLOG_SESSION_TIMEOUT": os.environ.get("SHERLOG_SESSION_TIMEOUT", "3600"),
        "GITHUB_TOKEN": "****" if os.environ.get("GITHUB_TOKEN") else "Not set",
        "GITHUB_MCP_IMAGE": os.environ.get("GITHUB_MCP_IMAGE", "ghcr.io/github/github-mcp-server"),
        "FILESYSTEM_MCP_IMAGE": os.environ.get("FILESYSTEM_MCP_IMAGE", "modelcontextprotocol/filesystem-mcp-server:latest"),
        "JIRA_URL": os.environ.get("JIRA_URL", "Not set"),
        "JIRA_API_TOKEN": "****" if os.environ.get("JIRA_API_TOKEN") else "Not set",
        "JIRA_EMAIL": "****" if os.environ.get("JIRA_EMAIL") else "Not set",
        "JIRA_MCP_IMAGE": os.environ.get("JIRA_MCP_IMAGE", "sooperset/mcp-atlassian:latest"),
    }
    
    logger.info("Environment configuration:")
    for key, value in env_vars.items():
        logger.info(f"  {key}: {value}")
    
    # Start session cleanup task
    await session_manager.start_cleanup_task()
    logger.info("Session cleanup task started")
    
    # Initialize connectors and generate tool proxies
    # Create a dummy context if needed for initialize_connectors
    class DummyContext:
        def __init__(self):
            self.request_id = "startup"
            self.request_metadata = {}
            self.user_identity = {}

    logger.info("Initializing connectors during startup...")
    connectors_initialized_successfully = await initialize_connectors(cast(Context, DummyContext()))
    
    if connectors_initialized_successfully:
        logger.info("Connectors initialized. Generating tool proxies...")
        try:
            proxies = await tool_proxy_generator.generate_all_proxies()
            logger.info(f"Successfully generated {len(proxies)} tool proxies during startup.")
            
            # Store these tools in a way that the /v1/initialize or FastMCP's initialize can access them
            # For FastMCP, this might involve registering them with the `mcp` instance.
            # For the custom /v1/initialize, it might involve populating a global list.
            # This part depends on how FastMCP and the custom /v1/initialize are meant to work together.
            # For now, let's assume FastMCP's @mcp.tool() decorators will pick them up if proxies are generated.

        except Exception as e:
            logger.error(f"Error generating tool proxies during startup: {e}")
            logger.info(f"Tool proxy generation error details: {traceback.format_exc()}")
    else:
        logger.warning("Connector initialization failed during startup. Some tools may not be available.")

    logger.info("Sherlog-REPL server startup complete")


# Define shutdown function without decorator
async def shutdown():
    """
    Shutdown handler for the MCP server.
    Clean up resources and stop background tasks.
    """
    logger.info("Sherlog-REPL server shutting down")
    
    await session_manager.stop_cleanup_task()
    logger.info("Session cleanup task stopped")
    
    # Shutdown all connectors
    if connectors:
        connector_names = ", ".join(connectors.keys())
        logger.info(f"Shutting down connectors: {connector_names}")
        
        for name, connector in connectors.items():
            try:
                await connector.shutdown()
                logger.info(f"Connector {name} shut down successfully")
            except Exception as e:
                logger.error(f"Error shutting down connector {name}: {e}")
    
    logger.info("Sherlog-REPL server shutdown complete")


# Define middleware function without decorator
async def request_logging_middleware(ctx: Context, next_handler):
    """
    Middleware for logging all requests and their timing.
    """
    request_id = f"{time.time():.0f}-{hash(ctx)}"
    setattr(ctx, 'request_id', request_id)
    
    # Log the request
    method = ctx.request_method if hasattr(ctx, 'request_method') else 'unknown'
    params = {}
    if hasattr(ctx, 'request_params'):
        # Mask sensitive information in parameters
        for key, value in ctx.request_params.items():
            if isinstance(value, str) and len(value) > 200:
                params[key] = f"{value[:200]}... ({len(value)} chars)"
            elif key in ['code', 'token', 'password', 'secret', 'key']:
                params[key] = f"****"
            else:
                params[key] = value
    
    logger.info(f"Request {request_id}: {method} {json.dumps(params)}")
    
    # Measure request timing
    start_time = time.time()
    
    try:
        # Call the next handler
        result = await next_handler()
        
        # Log the result
        end_time = time.time()
        duration = end_time - start_time
        
        result_summary = "No result"
        if result:
            if isinstance(result, list) and len(result) > 0:
                first_item = result[0]
                if hasattr(first_item, 'type') and first_item.type == 'text':
                    content = getattr(first_item, 'text', '')
                    result_summary = f"{content[:100]}... ({len(content)} chars)" if len(content) > 100 else content
            result_summary = str(result_summary)
        
        logger.info(f"Response {request_id}: completed in {duration:.2f}s")
        logger.info(f"Response {request_id} content: {result_summary}")
        
        return result
        
    except Exception as e:
        # Log any errors
        end_time = time.time()
        duration = end_time - start_time
        
        logger.error(f"Error {request_id}: {str(e)} (after {duration:.2f}s)")
        logger.info(f"Error {request_id} details: {traceback.format_exc()}")
        
        # Re-raise the exception
        raise


def main():
    """Start the FastMCP server using stdio transport (no Uvicorn needed)."""
    logger.info("Starting Sherlog-REPL FastMCP server via stdio transport")

    # This call blocks and handles lifecycle (startup/shutdown) internally
    # Transport can be switched via CLI/ENV later if required
    mcp.run("stdio")


if __name__ == "__main__":
    main()