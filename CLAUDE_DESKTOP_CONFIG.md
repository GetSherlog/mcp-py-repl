# Using MCP-PY-REPL with Claude Desktop

This guide explains how to configure Claude Desktop to use the MCP-PY-REPL server, which provides Python REPL capabilities.

## Option 1: Running Locally

### Prerequisites
- Python 3.10 or higher
- Claude Desktop

### Setup

1. Install the package:
   ```bash
   pip install -e .
   ```

2. Start the server:
   ```bash
   python -m mcp_py_repl
   ```

3. Configure Claude Desktop:
   - Go to Settings > Model Control Protocol
   - Add a new server configuration:
     - Name: `Python REPL`
     - URL: `http://localhost:8080`
     - Authentication: None

## Option 2: Using Docker (Pre-built Image)

### Prerequisites
- Docker Desktop installed and running
- Claude Desktop
- GitHub authentication for Container Registry (for private images)

### Setup

1. Configure Claude Desktop with this JSON configuration:

```json
{
  "mcp": {
    "servers": {
      "python-repl": {
        "command": "docker",
        "args": [
          "run",
          "-i",
          "--rm",
          "--mount", "type=bind,src=${workspaceFolder},dst=/projects/workspace",
          "ghcr.io/evalstate/sherlog-repl:latest"
        ]
      }
    }
  }
}
```

## Option 3: Using Locally-Built Docker Image

### Prerequisites
- Docker Desktop installed and running
- Claude Desktop

### Setup

1. Build the Docker image locally from the repo:
   ```bash
   # Navigate to the repository directory
   cd /path/to/mcp-py-repl

   # Build the Docker image
   docker build -t sherlog-repl-local .
   ```

2. Configure Claude Desktop with this JSON configuration (modify your settings.json):

```json
{
  "mcp": {
    "servers": {
      "python-repl": {
        "command": "docker",
        "args": [
          "run",
          "-i",
          "--rm",
          "--mount", "type=bind,src=${workspaceFolder},dst=/projects/workspace",
          "sherlog-repl-local"
        ]
      }
    }
  }
}
```

## Option 4: Using Sherlog-REPL (Advanced)

For a more feature-rich experience with access to GitHub, filesystem, and Jira tools:

### Prerequisites
- Docker Desktop installed and running
- Claude Desktop
- (Optional) GitHub personal access token
- (Optional) Jira account credentials

### Setup

1. Configure Claude Desktop with this JSON configuration:

```json
{
  "mcp": {
    "servers": {
      "sherlog-repl": {
        "command": "docker",
        "args": [
          "run",
          "-i",
          "--rm",
          "-e", "GITHUB_TOKEN=${env:GITHUB_TOKEN}",
          "-e", "JIRA_URL=${env:JIRA_URL}",
          "-e", "JIRA_API_TOKEN=${env:JIRA_API_TOKEN}",
          "-e", "JIRA_EMAIL=${env:JIRA_EMAIL}",
          "--mount", "type=bind,src=${workspaceFolder},dst=/projects/workspace",
          "ghcr.io/evalstate/sherlog-repl:latest"
        ]
      }
    }
  }
}
```

2. Set environment variables in your shell or `.env` file:
   ```bash
   export GITHUB_TOKEN=your_github_token
   export JIRA_URL=https://your-instance.atlassian.net
   export JIRA_API_TOKEN=your_jira_token
   export JIRA_EMAIL=your_email@example.com
   ```

## Using with Claude Desktop

Once configured, you can interact with the Python REPL:

1. Basic Python execution:
   ```
   Can you help me calculate the fibonacci sequence using the Python REPL?
   ```

2. For Sherlog-REPL, register and use tools:
   ```
   Using Sherlog-REPL, please register filesystem tools and show me the files in my current directory.
   ```

## Available Tools

### Basic Python REPL
- `execute_python`: Run Python code
- `install_package`: Install a Python package
- `list_variables`: List all variables in session

### Sherlog-REPL (Advanced)
- All basic Python REPL tools
- Filesystem tools: list files, read/write files, etc.
- GitHub tools (if configured): repositories, issues, etc.
- Jira tools (if configured): projects, issues, etc.

## Troubleshooting

If you encounter issues:

1. Verify Docker is running:
   ```bash
   docker info
   ```

2. Test the server manually:
   ```bash
   curl -X POST http://localhost:8080/v1/list_tools \
     -H 'Content-Type: application/json' \
     -d '{}'
   ```

3. Check logs by running with environment variables:
   ```bash
   SHERLOG_LOG_LEVEL=DEBUG sherlog-repl
   ```