Trigger the QA testware preparation agent for one or more Jira stories.

## What this does
1. Reads the Jira story title and description
2. Generates test cases with Claude AI
3. Creates AIO test cases, test set, and links to your cycle
4. Writes a Playwright spec skeleton
5. Creates Jira QA subtasks and marks Test Case Writing as Done

## How to use

Ask the user for:
- **Cycle name** (e.g. "squad-a-sprint-35") — required
- **Issue keys** (e.g. "TAT-5800, TAT-5801") — required
- **Platform URL** — default is http://localhost:8000, ask if they want to use a different deployed URL

Then call the platform API:

```
POST {platform_url}/api/testware/kickoff
{
  "issues": ["TAT-XXXX"],
  "cycle_name": "cycle-name-here",
  "use_knowledge": true
}
```

Stream the response from:
```
GET {platform_url}/api/testware/stream/{job_id}
```

Report each log line to the user as it arrives so they can see real-time progress.

When done, summarize:
- Which AIO test cases were created
- Which Jira subtasks were created
- Where the Playwright spec was written

## Example invocation

User: /project:testware-prep
You: What is the cycle name and which Jira issue keys should I process?
User: cycle squad-a-sprint-35, issues TAT-5800 TAT-5801
You: [call API, stream logs, report results]

## Platform URL

Default: http://localhost:8000
For cloud deployment: ask the user for their deployed URL or check if PLATFORM_URL is set in the environment.
