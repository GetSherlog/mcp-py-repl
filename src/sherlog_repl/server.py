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
from contextlib import redirect_stdout, redirect_stderr
from typing import Any, Dict, List, Optional, Set, Union

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
handlers = [logging.StreamHandler()]
if LOG_FILE:
    handlers.append(logging.FileHandler(LOG_FILE))

# Set up root logger
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=handlers
)
logger = logging.getLogger("sherlog_repl")

# Initialize the MCP server
mcp = FastMCP(
    "sherlog-repl",
    log_level=LOG_LEVEL
)

# Global state
session_manager = SessionManager()
tool_proxy_generator = ToolProxyGenerator()
concurrency_manager = ConcurrencyManager()

# Variable to store connector instances
connectors = {}


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
        logger.debug("Connectors already initialized, skipping initialization")
        return True
    
    logger.info("Starting connector initialization")
    
    # Create connector instances with configuration
    github_config = {
        "github_token": os.environ.get("GITHUB_TOKEN"),
        "container_image": os.environ.get("GITHUB_MCP_IMAGE", "github/github-mcp-server:latest")
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
            
    logger.debug(f"GitHub connector config: {safe_github_config}")
    logger.debug(f"Filesystem connector config: {filesystem_config}")
    logger.debug(f"Jira connector config: {safe_jira_config}")
    
    # Initialize connectors
    connectors = {
        "github": GitHubConnector(github_config),
        "filesystem": FilesystemConnector(filesystem_config),
        "jira": JiraConnector(jira_config)
    }
    
    # Register connectors with the tool proxy generator
    for connector in connectors.values():
        tool_proxy_generator.register_connector(connector)
        logger.debug(f"Registered {connector.name} connector with tool proxy generator")
    
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
            logger.debug(f"Tool proxy error details: {traceback.format_exc()}")
    
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
        logger.debug(f"User context for session {session_id}: {user_context}")
    
    # Get or create the session
    start_time = time.time()
    session = await session_manager.get_or_create_session(session_id, user_context)
    end_time = time.time()
    
    if end_time - start_time > 0.1:  # Log only if it took more than 100ms
        logger.debug(f"Get or create session {session_id} took {end_time - start_time:.2f} seconds")
    
    return session


@mcp.tool()
async def execute_python(ctx: Context, code: str, reset: bool = False, register_tools: List[str] = None) -> List[types.TextContent]:
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
    logger.debug(f"[{request_id}] Code to execute: {code[:200]}{'...' if len(code) > 200 else ''}")
    
    start_time = time.time()
    
    # Initialize connectors if needed
    if not connectors:
        logger.info(f"[{request_id}] Initializing connectors for first time")
        success = await initialize_connectors(ctx)
        if not success:
            logger.warning(f"[{request_id}] Not all connectors initialized successfully")
            return [types.TextContent(type="text", text="WARNING: Not all MCP connectors could be initialized. Some tools may not be available.")]
    
    # Get or create the session
    session = await get_or_create_session(ctx)
    logger.debug(f"[{request_id}] Using session: {session.id} (age: {time.time() - session.created_at.timestamp():.1f}s)")
    
    # Handle reset
    if reset:
        logger.info(f"[{request_id}] Resetting session {session.id}")
        session.clear()
        return [types.TextContent(type="text", text="Python session reset. All variables cleared.")]
    
    # Register tools if requested
    if register_tools:
        logger.info(f"[{request_id}] Registering tools: {register_tools}")
        for domain in register_tools:
            if domain not in connectors:
                logger.warning(f"[{request_id}] Unknown domain requested: {domain}")
                return [types.TextContent(type="text", text=f"Unknown domain: {domain}")]
            
            # Check if connector is initialized
            if not connectors[domain].initialized:
                logger.info(f"[{request_id}] Connector {domain} not initialized, attempting initialization")
                await connectors[domain].initialize()
                if not connectors[domain].initialized:
                    logger.warning(f"[{request_id}] Failed to initialize connector: {domain}")
                    return [types.TextContent(type="text", text=f"Cannot register tools from {domain}. The connector could not be initialized.")]
            
            # Register all tools from the domain
            domain_start = time.time()
            await tool_proxy_generator.discover_tools(domain)
            count = tool_proxy_generator.register_proxies_in_namespace(session.namespace)
            domain_end = time.time()
            logger.info(f"[{request_id}] Registered {count} tools from {domain} in session {session.id} ({domain_end - domain_start:.2f}s)")
    
    # Execute the code in the session
    execution_start = time.time()
    async with session.lock:
        # Redirect stdout and stderr
        stdout = io.StringIO()
        stderr = io.StringIO()
        
        try:
            logger.debug(f"[{request_id}] Executing code in session {session.id}")
            with redirect_stdout(stdout), redirect_stderr(stderr):
                # Run the code in the session's namespace
                exec(code, session.namespace)
            
            output = stdout.getvalue()
            errors = stderr.getvalue()
            
            # Record in session history
            result_text = output or errors or "Code executed successfully (no output)"
            session.add_to_history(code, result_text)
            
            # Prepare the response
            result = ""
            if output:
                result += f"Output:\n{output}"
                logger.debug(f"[{request_id}] Code produced output: {len(output)} chars")
            if errors:
                result += f"\nErrors:\n{errors}"
                logger.warning(f"[{request_id}] Code produced errors: {errors}")
            if not output and not errors:
                try:
                    # Try to evaluate the last line for expression results
                    last_line = code.strip().split('\n')[-1]
                    last_value = eval(last_line, session.namespace)
                    result = f"Result: {repr(last_value)}"
                    logger.debug(f"[{request_id}] Evaluated expression result: {repr(last_value)[:100]}{'...' if len(repr(last_value)) > 100 else ''}")
                except (SyntaxError, ValueError, NameError):
                    result = "Code executed successfully (no output)"
                    logger.debug(f"[{request_id}] Code executed with no output")
            
            execution_end = time.time()
            total_time = execution_end - start_time
            execution_time = execution_end - execution_start
            logger.info(f"[{request_id}] execute_python completed in {total_time:.2f}s (execution: {execution_time:.2f}s)")
            
            return [types.TextContent(type="text", text=result)]
                
        except Exception as e:
            error_msg = f"Error executing code:\n{traceback.format_exc()}"
            logger.error(f"[{request_id}] Failed to execute code: {str(e)}")
            logger.debug(f"[{request_id}] Error details: {traceback.format_exc()}")
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
            logger.debug(f"[{request_id}] Listing tools for specific domain: {domain}")
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
            logger.debug(f"[{request_id}] Listing tools for all domains")
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
            result += f"## {domain.capitalize()} Tools\n\n"
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
                logger.debug(f"[{request_id}] Connector not available: {name}")
        
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
        logger.debug(f"[{request_id}] Error details: {traceback.format_exc()}")
        return [types.TextContent(type="text", text=error_msg)]


@mcp.tool()
async def register_tools(ctx: Context, domains: List[str]) -> List[types.TextContent]:
    """
    Register tools from specified domains in the current session.
    
    Args:
        ctx: The context for the MCP call.
        domains: List of domains to register tools from (e.g., ["github", "filesystem"]).
        
    Returns:
        A confirmation message.
    """
    request_id = getattr(ctx, 'request_id', 'unknown')
    logger.info(f"[{request_id}] register_tools called: domains={domains}")
    
    start_time = time.time()
    
    # Initialize connectors if needed
    if not connectors:
        logger.info(f"[{request_id}] Initializing connectors for first time")
        success = await initialize_connectors(ctx)
        if not success:
            logger.warning(f"[{request_id}] Not all connectors initialized successfully")
            return [types.TextContent(type="text", text="WARNING: Not all MCP connectors could be initialized. Some tools may not be available.")]
    
    # Get or create the session
    session = await get_or_create_session(ctx)
    logger.debug(f"[{request_id}] Using session: {session.id} (age: {time.time() - session.created_at.timestamp():.1f}s)")
    
    # Validate domain names
    invalid_domains = [d for d in domains if d not in connectors]
    if invalid_domains:
        logger.warning(f"[{request_id}] Unknown domains requested: {invalid_domains}")
        return [types.TextContent(type="text", text=f"Unknown domains: {', '.join(invalid_domains)}")]
    
    # Register tools from each domain
    registered_counts = {}
    unavailable = []
    
    try:
        async with session.lock:
            for domain in domains:
                logger.debug(f"[{request_id}] Attempting to register tools from domain: {domain}")
                # Check if connector is initialized
                if not connectors[domain].initialized:
                    # Try to initialize it one more time
                    logger.debug(f"[{request_id}] Connector {domain} not initialized, attempting initialization")
                    success = await connectors[domain].initialize()
                    if not success:
                        logger.warning(f"[{request_id}] Failed to initialize connector: {domain}")
                        unavailable.append(domain)
                        continue
                
                # Discover tools from the domain
                domain_start = time.time()
                await tool_proxy_generator.discover_tools(domain)
                
                # Register the tools in the session namespace
                count = tool_proxy_generator.register_proxies_in_namespace(session.namespace)
                domain_end = time.time()
                registered_counts[domain] = count
                logger.info(f"[{request_id}] Registered {count} tools from {domain} in {domain_end - domain_start:.2f}s")
        
        # Format the result
        result = "Tools registered in current session:\n\n"
        for domain, count in registered_counts.items():
            result += f"- {domain}: {count} tools\n"
        
        # Add information about unavailable domains
        if unavailable:
            result += "\nThe following domains could not be initialized:\n\n"
            for domain in unavailable:
                if domain == "github":
                    result += "- **GitHub**: To enable GitHub tools, set the GITHUB_TOKEN environment variable.\n"
                elif domain == "jira":
                    result += "- **Jira**: To enable Jira tools, set the JIRA_URL, JIRA_API_TOKEN, and JIRA_EMAIL environment variables.\n"
                else:
                    result += f"- **{domain.capitalize()}**: Not configured properly.\n"
        
        end_time = time.time()
        logger.info(f"[{request_id}] register_tools completed in {end_time - start_time:.2f}s")
        
        return [types.TextContent(type="text", text=result)]
    
    except Exception as e:
        error_msg = f"Error registering tools: {str(e)}"
        logger.error(f"[{request_id}] Failed to register tools: {str(e)}")
        logger.debug(f"[{request_id}] Error details: {traceback.format_exc()}")
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
    logger.debug(f"[{request_id}] Using session: {session.id} (age: {time.time() - session.created_at.timestamp():.1f}s)")
    
    # Get all variables
    vars_dict = {
        k: repr(v) for k, v in session.namespace.items() 
        if not k.startswith('_') and k != '__builtins__'
    }
    
    if not vars_dict:
        logger.debug(f"[{request_id}] No variables found in session {session.id}")
        return [types.TextContent(type="text", text="No variables in current session.")]
    
    var_list = "\n".join(f"{k} = {v}" for k, v in vars_dict.items())
    logger.debug(f"[{request_id}] Found {len(vars_dict)} variables in session {session.id}")
    
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
    logger.debug(f"[{request_id}] Using session: {session.id} (age: {time.time() - session.created_at.timestamp():.1f}s)")
    
    # Clear the session
    session.clear()
    logger.info(f"[{request_id}] Session {session.id} cleared")
    
    end_time = time.time()
    logger.info(f"[{request_id}] reset_session completed in {end_time - start_time:.2f}s")
    
    return [types.TextContent(type="text", text="Session reset. All variables cleared.")]


@mcp.on_startup()
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
        "GITHUB_MCP_IMAGE": os.environ.get("GITHUB_MCP_IMAGE", "github/github-mcp-server:latest"),
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
    
    logger.info("Sherlog-REPL server startup complete")


@mcp.on_shutdown()
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


@mcp.middleware()
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
        logger.debug(f"Response {request_id} content: {result_summary}")
        
        return result
        
    except Exception as e:
        # Log any errors
        end_time = time.time()
        duration = end_time - start_time
        
        logger.error(f"Error {request_id}: {str(e)} (after {duration:.2f}s)")
        logger.debug(f"Error {request_id} details: {traceback.format_exc()}")
        
        # Re-raise the exception
        raise


def main():
    """
    Main entry point for the server.
    """
    logger.info("Starting Sherlog-REPL server")
    mcp.run()


if __name__ == "__main__":
    main()