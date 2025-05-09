# Setting Up Sherlog-Canvas Unified REPL MCP Server for Claude Desktop

![Sherlog-Canvas Unified REPL](https://raw.githubusercontent.com/evalstate/sherlog-repl/main/docs/images/sherlog-repl-logo.png)

This guide will walk you through setting up the Sherlog-Canvas Unified REPL MCP Server to work with Claude Desktop (or other Claude interfaces that support MCP protocol).

## Prerequisites

- Docker Desktop installed and running
- Python 3.10 or higher
- Claude Desktop (or other Claude interface with MCP support)
- Git (optional, for GitHub tools)
- Jira account (optional, for Jira tools)

## Installation

### 1. Clone and Build the Sherlog-REPL

```bash
# Clone this repository
git clone https://github.com/evalstate/sherlog-repl.git
cd sherlog-repl

# Build and install the package
pip install -e .
```

### 2. Setting Up Environment Variables

Create a `.env` file in your home directory or project directory with the following variables:

```
# Required for GitHub tools (optional)
GITHUB_TOKEN=your_github_personal_access_token

# Required for Jira tools (optional)
JIRA_URL=https://your-instance.atlassian.net
JIRA_API_TOKEN=your_jira_api_token
JIRA_EMAIL=your_jira_account_email
```

> Note: You don't need to set all environment variables. If you don't set the GitHub token, GitHub tools won't be available. Similarly, if you don't set Jira credentials, Jira tools won't be available. Filesystem tools will always be available regardless of environment variables.

### 3. Running the Sherlog-REPL Server

```bash
# Start the server
sherlog-repl
```

By default, this will start the server on port 8080 and listen for MCP requests.

## Setting Up Claude Desktop

### 1. Configure Claude Desktop to Use Sherlog-REPL

1. Open Claude Desktop
2. Go to Settings > Model Control Protocol
3. Enable MCP
4. Add a new MCP server configuration:
   - Name: `Sherlog-REPL`
   - URL: `http://localhost:8080`
   - Authentication: None (or configure as needed)

### 2. Using Sherlog-REPL with Claude

Once configured, you can use Claude Desktop to interact with the Sherlog-REPL server. Here are some example prompts:

#### Register tools and use them
```
Can you help me explore my filesystem using Sherlog-REPL? First, register the filesystem tools, then list the files in my current directory.
```

#### Execute Python code with filesystem operations
```
Using Sherlog-REPL, can you write a Python script that lists all Python files in the current directory, reads their content, and counts the total lines of code?
```

#### GitHub operations (if configured)
```
Using Sherlog-REPL with GitHub tools, can you show me the latest issues from my repository [repository-name]?
```

## Available Tools

### Main Tools

1. `execute_python`: Run Python code with a persistent state
2. `list_available_tools`: See what tools are available from connected MCPs
3. `register_tools`: Register tools from specific domains in your session
4. `list_session_variables`: View all variables in your current session
5. `reset_session`: Clear all variables in your session

### Domain-Specific Tools

When you register domain-specific tools using `register_tools`, you'll get access to:

1. **Filesystem Tools**:
   - `filesystem_list_files`
   - `filesystem_read_file`
   - `filesystem_write_file`
   - ... and more

2. **GitHub Tools** (if GitHub token is configured):
   - `github_list_repositories`
   - `github_list_issues`
   - `github_create_issue`
   - ... and more

3. **Jira Tools** (if Jira credentials are configured):
   - `jira_list_projects`
   - `jira_list_issues`
   - `jira_create_issue`
   - ... and more

## Example Workflow

1. Start a session in Claude Desktop
2. Ask Claude to register filesystem tools:
   ```
   Please use Sherlog-REPL to register filesystem tools and list files in my directory.
   ```

3. Claude will execute:
   ```python
   # Register filesystem tools
   await register_tools(domains=["filesystem"])
   
   # List files in the current directory
   files = await filesystem_list_files(path="/path/to/current/dir")
   for file in files:
       print(file['name'])
   ```

4. Continue working with the same variables:
   ```
   Now let's filter for only Python files and count their lines.
   ```

5. Claude will execute code with access to the previously defined `files` variable:
   ```python
   # Filter for Python files
   python_files = [f for f in files if f['name'].endswith('.py')]
   
   # Count lines in each Python file
   total_lines = 0
   for py_file in python_files:
       content = await filesystem_read_file(path=py_file['path'])
       line_count = len(content.splitlines())
       print(f"{py_file['name']}: {line_count} lines")
       total_lines += line_count
       
   print(f"Total: {total_lines} lines of Python code")
   ```

## Enhanced Logging

Sherlog-REPL provides detailed logging to help with debugging and monitoring. You can configure logging using environment variables:

```bash
# Set the log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
export SHERLOG_LOG_LEVEL=DEBUG

# Optional: Save logs to a file
export SHERLOG_LOG_FILE=/path/to/sherlog-repl.log

# Start the server with verbose logging
sherlog-repl
```

The logs include:
- Request details with unique request IDs
- Execution times for each operation
- Tool registrations and executions
- Session management activities
- Error details with stack traces (in DEBUG mode)

Example log output:
```
2025-05-09 10:15:30,123 - sherlog_repl - INFO - Request 1683630930-12345: execute_python {"reset": false, "register_tools": ["filesystem"], "code": "****"}
2025-05-09 10:15:30,456 - sherlog_repl - INFO - [1683630930-12345] Registered 8 tools from filesystem in 0.12s
2025-05-09 10:15:30,789 - sherlog_repl - INFO - [1683630930-12345] execute_python completed in 0.66s (execution: 0.33s)
```

## Troubleshooting

### Docker Issues

If you encounter problems with Docker containers:

```bash
# Check if Docker is running
docker info

# Pull the required images in advance
docker pull github/github-mcp-server:latest
docker pull modelcontextprotocol/filesystem-mcp-server:latest 
docker pull sooperset/mcp-atlassian:latest
```

### Connection Issues

If Claude cannot connect to the Sherlog-REPL server:

1. Check if the server is running:
   ```bash
   ps aux | grep sherlog-repl
   ```

2. Test the server manually:
   ```bash
   curl -X POST http://localhost:8080/v1/list_tools \
     -H 'Content-Type: application/json' \
     -d '{}'
   ```

### Tool Registration Issues

If specific tools aren't showing up:

1. Check environment variables:
   ```bash
   echo $GITHUB_TOKEN
   echo $JIRA_URL
   ```

2. Try listing available tools:
   ```
   Please use Sherlog-REPL to list all available tools.
   ```

## Advanced Configuration

### Custom Docker Images

You can specify custom Docker images for each MCP connector by setting environment variables:

```bash
export GITHUB_MCP_IMAGE=your-custom-github-mcp:tag
export FILESYSTEM_MCP_IMAGE=your-custom-filesystem-mcp:tag
export JIRA_MCP_IMAGE=your-custom-jira-mcp:tag
```

### Persistent Sessions

By default, sessions expire after 1 hour of inactivity. You can change this by setting:

```bash
export SHERLOG_SESSION_TIMEOUT=7200  # 2 hours in seconds
```

## Security Considerations

- The Sherlog-REPL server provides full filesystem access to whoever can connect to it
- Environment variables may contain sensitive tokens
- Consider running the server in a container or virtual machine for isolation
- Use caution when allowing external access to the server