# Settings and API keys

Open **Settings** from the account menu at the top right.

## Account and workspace

**Account** edits your name, email and sign-in methods. **Workspace** shows the current workspace and its members and roles; organization admins invite people there. Switch workspaces from the menu in the navigation bar.

## API keys

An API key lets the command-line tool act for your workspace. Under **API keys**, choose **New key**, then set:

| Field      | Choices                                                                                             |
| ---------- | --------------------------------------------------------------------------------------------------- |
| Name       | Any label, for example `laptop` or `CI runner`.                                                     |
| Scope      | **Full** (everything), **Tasks** (run trials, create tasks, read files) or **Read** (read-only).    |
| Expiration | **Never expires**, **7 days**, **30 days**, **90 days** or **1 year**.                              |

**Create key** shows the key once. Copy it then; Oddish cannot show it again. Each key's row shows when it was last used, and the revoke button stops it immediately. A view-only workspace can create read keys only.

## Your API key and notifications

**Your API key** stores your own Anthropic API key. When Abundant enables it for you, your runs use it instead of the platform key. **Notifications** chooses which Slack alerts you receive, such as **Experiment failed** or **QA verdict rejected or unavailable**, and the dollar cutoffs for cost alerts.

Next: [Run a task](first-run.md).
