# VS Code GitHub MCP Setup

## What this is
This workspace now includes a local VS Code MCP configuration at `.vscode/mcp.json`.

The configuration uses GitHub's official MCP server Docker image:

- `ghcr.io/github/github-mcp-server:v0.33.0`

It is set up to ask for a GitHub Personal Access Token when the server starts, instead of saving the token in a file.

## What Codex already prepared
- Installed GitHub CLI and confirmed GitHub authentication works.
- Set Git and `gh` to use `code --wait` as the editor.
- Added a local workspace MCP configuration for GitHub.

## Important safety note
The GitHub token that was pasted into chat should be treated as exposed.

Recommended next step:

1. Use it only long enough to confirm the MCP flow works.
2. Revoke it in GitHub.
3. Create a fresh token for ongoing use.

## Why install MCP if Codex already does most work?
MCP does not replace coding. It reduces the amount of GitHub context that you have to manually copy into chat.

Practical benefits:

1. VS Code can let an agent read GitHub issues and pull requests directly.
2. You do less copy-paste between browser tabs and chat.
3. The agent can work with GitHub context in the same workspace where you code.
4. Future tools like Notion or Jira can use the same pattern.

Important limitation:

This workspace config helps MCP-aware agents inside VS Code. It does not automatically change every terminal-based Codex session.

## Beginner-friendly mental model
- `git` moves code.
- `gh` talks to GitHub from the terminal.
- `MCP` lets an AI tool talk to GitHub as a tool.

So:

- use `git` for commits and branches
- use `gh` for PRs and issues
- use `MCP` when you want the AI to understand GitHub context directly

## What you need to do next
1. Start Docker Desktop.
2. Reopen VS Code in this workspace if it was already open.
3. Open the Command Palette.
4. Run `MCP: List Servers`.
5. Start the `github` server if it is not already started.
6. When VS Code prompts for a token, paste a valid GitHub Personal Access Token.
7. Approve the trust prompt for the server after checking the configuration.

## How to tell whether it worked
Signs of success:

1. `MCP: List Servers` shows `github` as available.
2. The server status changes to running.
3. Chat tools show GitHub-related capabilities.

If it fails:

1. Make sure Docker Desktop is running.
2. Run `MCP: List Servers` and inspect the server output.
3. Confirm the token still has `repo`, `read:org`, and `workflow` scopes.

## Why the token is not hardcoded
VS Code's official guidance is to avoid hardcoding secrets in `mcp.json`.

This setup follows that rule by using:

- `inputs.promptString`
- a password-style prompt
- runtime environment injection into the MCP server

## What was updated
- The Docker image tag is pinned to `v0.33.0`.
- The image has been pre-pulled locally, so the first VS Code run should be quieter.

## Sources
- GitHub official MCP server: https://github.com/github/github-mcp-server
- VS Code MCP configuration docs: https://code.visualstudio.com/docs/copilot/customization/mcp-servers
