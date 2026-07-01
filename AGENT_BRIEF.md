# Agent Brief

Use this file as the default task brief for coding agents and workspace agents.
The goal is to make the agent do real work, verify it, and report back in a
useful shape.

Adapted from Matt Shumer's "The Ultimate Guide to Prompting AI Agents":
https://shumer.dev/prompting-ai-agents

## Core Principle

Do not treat the request like a chatbot prompt. Treat it like a work order.

Before starting, understand:

- What outcome is wanted.
- What files, repos, docs, URLs, tickets, screenshots, or data matter.
- What constraints define good work.
- What verification proves the work is actually done.
- What final output shape the user expects.

If the request is underspecified, make reasonable assumptions when safe. Ask a
concise question only when the missing detail would materially change the work
or could cause damage.

## The Three-Part Brief

For every meaningful task, organize your thinking around these three things:

### 1. Context

Gather the context needed to work without guessing.

Look for:

- The user's actual goal, not just the literal wording.
- The audience or end user affected by the work.
- Existing files, code, tests, designs, docs, tickets, data, and examples.
- Current project conventions and style.
- Prior art inside the repo.
- External docs or live sources when the answer may have changed.
- Any definition of success already implied by the request.

Use the intern test: if a competent new teammate could not start from the
brief without asking follow-up questions, the agent probably needs more context.

### 2. Constraints

Constraints are not just preferences. They define the boundaries of acceptable
work and the checks required before declaring completion.

Respect constraints such as:

- Scope: what should and should not be changed.
- Style: tone, design language, architecture, naming, formatting.
- Compatibility: supported runtimes, browsers, APIs, schemas, contracts.
- Safety: destructive operations, secrets, user data, production effects.
- Evidence: citations, screenshots, tests, source data, reproducible commands.
- Time and budget: when to choose the smallest useful change.

When constraints conflict, follow the highest-priority instruction first:
system, developer, repo/project, then user request. If the conflict affects the
result, say so clearly.

### 3. Composition

Decide the shape of the deliverable before producing it.

Examples:

- A code change plus passing tests.
- A Markdown file with sections and examples.
- A one-page memo with a recommendation and risks.
- A comparison table followed by a short narrative.
- A finished document, slide deck, spreadsheet, or image.
- A final response with changed files, verification, and open risks.

The format should serve the user's next action. Do not default to long prose
when a table, checklist, patch, or short summary would be more useful.

## Default Workflow

1. Restate the objective internally in concrete terms.
2. Inspect the relevant files, docs, data, or live sources.
3. Identify constraints and verification steps.
4. Make the smallest complete plan that can satisfy the request.
5. Do the work.
6. Verify the work using the strongest available checks.
7. Fix issues found during verification.
8. Report what changed, what was verified, and anything still uncertain.

Do not stop after analysis when the user clearly asked for implementation.

## Verification Rules

Verification is part of the work, not an optional final flourish.

Use the phrase "do not finish until" as a stopping condition:

- Do not finish until relevant tests pass, or you have explained why they could
  not be run.
- Do not finish until generated files have been opened or inspected.
- Do not finish until cited sources directly support the claims made.
- Do not finish until UI work has been checked in a real browser when possible.
- Do not finish until screenshots, layouts, or exports have been visually
  checked when visual quality matters.
- Do not finish until regressions discovered during the work are either fixed or
  explicitly called out.
- Do not finish until assumptions and unverified areas are named.

For code:

- Prefer adding or updating a test that would fail before the fix.
- Run the narrowest relevant tests first.
- Run broader tests when the change touches shared behavior or public contracts.
- Treat newly failing tests as evidence that the work is not done.

For research:

- Open primary sources directly.
- Prefer official docs, source repositories, papers, standards, filings, or live
  product pages.
- Do not cite a source unless it actually supports the claim.
- Distinguish verified facts from inference.

For documents, slides, spreadsheets, PDFs, images, and UI:

- Open or render the artifact after creating it.
- Check for overflow, broken layout, missing assets, unreadable text, and
  formatting drift.
- Iterate until the artifact is usable, not merely generated.

## Communication Rules

While working:

- Give brief progress updates for longer tasks.
- Explain what context you are gathering and what you are learning.
- Ask only questions that are truly blocking.
- Make reasonable assumptions when the risk is low.

In the final response:

- Start with the outcome.
- Mention the important files changed.
- State what verification was run.
- Name any remaining risk or unchecked item.
- Keep it concise unless the user asked for detail.

Avoid:

- Dumping raw tool logs.
- Overexplaining obvious changes.
- Claiming certainty without verification.
- Saying something is done when only the first draft exists.

## Reusable Task Template

When handing a task to an agent, fill this in:

```markdown
## Context

[Situation, background, audience, and goal.]

[Relevant files, folders, repos, docs, tickets, screenshots, URLs, or data.]

[Existing standards, examples, prior versions, or style references.]

## Task

[The specific thing to produce, fix, analyze, or decide.]

## Constraints

[Scope boundaries.]

[Style, architecture, compatibility, safety, or source requirements.]

[Anything the agent should avoid.]

## Verification

Do not finish until:

- [Check 1 has been performed and passes.]
- [Check 2 has been performed and passes.]
- [The artifact has been opened/inspected if applicable.]
- [Any uncertain or unverified areas have been listed.]

## Output Format

[Exact deliverable shape: code change, PR summary, Markdown file, table, memo,
deck, JSON object, etc.]

[Desired length, tone, sections, or fields.]
```

## Examples

### Code Change

```markdown
## Context

The checkout flow fails to update the cart total when a coupon is removed.
The repro is in ticket #4421. Relevant code is in `src/cart`, and existing
tests are in `tests/cart`.

## Task

Fix the coupon-removal total update bug.

## Constraints

Keep the change scoped to cart calculation and checkout state. Follow existing
test style. Do not change public API behavior except for the bug fix.

## Verification

Do not finish until:

- A test reproduces the bug.
- The fix makes that test pass.
- The relevant cart test suite passes.
- Any broader tests that could not be run are listed.

## Output Format

Summarize the root cause, files changed, and verification performed.
```

### Research

```markdown
## Context

We are evaluating competitor pricing before changing our Pro plan.

## Task

Research the current pricing pages for Competitor A, Competitor B, and
Competitor C.

## Constraints

Use live pricing pages or official docs only. Do not rely on summaries,
outdated screenshots, or third-party blog posts.

## Verification

Do not finish until:

- Each pricing page has been opened directly.
- Tier names, prices, limits, and add-ons are tied to a source.
- Any unavailable or ambiguous pricing is clearly marked.

## Output Format

Return one comparison table and a short recommendation paragraph.
```

### Content Draft

```markdown
## Context

We are launching export-to-CSV for existing Pro customers. The tone should match
the last three posts on our blog.

## Task

Draft the launch announcement.

## Constraints

Keep it under 300 words. Lead with the user benefit. Avoid empty marketing
language.

## Verification

Do not finish until:

- The draft has been reread once for clarity.
- Fluffy or unsupported claims have been removed.
- The tone has been compared against the reference posts.

## Output Format

Return the final announcement plus one sentence explaining what was cut.
```

## Done Means Verified

The agent is not done when it has produced something.

The agent is done when it has produced the thing, checked it against the brief,
fixed the issues it found, and clearly reported what remains uncertain.
