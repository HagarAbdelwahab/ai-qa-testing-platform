Trigger the QA testware preparation agent for one or more Jira stories.

## What this does
1. Reads the Jira story: description, every comment (oldest first), linked tickets, context-repo rules
2. Drafts test cases with Claude — nothing is written to AIO yet
3. You review the draft; only the approved cases are created in AIO, linked to the story,
   published, and added to the story's test set and the cycle

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

The `done` event's `result.drafts` holds, per story, `{issue_key, story_title, cases, existing}`
(`cases` excludes titles AIO already has). Show the cases as a numbered list per story and WAIT
for the user to approve or drop items. Then, per story, send only the approved cases:

```
POST {platform_url}/api/agents/add-cases
{"issue_key": "TAT-XXXX", "cycle_name": "cycle-name-here", "cases": [ ...approved case objects... ]}
```

Stream `GET {platform_url}/api/agents/stream/{job_id}`. When done, summarize the AIO keys in
`result.added_cases` per story. Never call add-cases without the user's approval.

## Example invocation

User: /project:testware-prep
You: What is the cycle name and which Jira issue keys should I process?
User: cycle squad-a-sprint-35, issues TAT-5800 TAT-5801
You: [call kickoff, stream logs, show drafts, wait for approval, call add-cases, report AIO keys]

## Platform URL

Default: http://localhost:8000
For cloud deployment: ask the user for their deployed URL or check if PLATFORM_URL is set in the environment.
