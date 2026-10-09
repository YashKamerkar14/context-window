# Fact-checker agreement

Evaluated 2026-10-08 with `cw factcheck eval` (judge: `anthropic/claude-haiku-5.5`).

**Agreement: 59 of 60 (98%).** The target is 90%.

Unsupported claims kept out of print (judged unsupported or unclear): 40 of 40.

| Kind | Label | Claims | Agreed | Judged supported | Unsupported | Unclear |
|---|---|---|---|---|---|---|
| original | supported | 20 | 19 | 19 | 1 | 0 |
| false | unsupported | 20 | 20 | 0 | 20 | 0 |
| altered | unsupported | 20 | 20 | 0 | 20 | 0 |

Escalated to the strong model as `unclear`: 0.

## Disagreements

- Claim 7 (original, labelled supported): judged unsupported. "Orion AI is a multi-agent system that uses Amazon Bedrock and Strands Agents, an open source agent orchestration framework from AWS." Reason given: The source confirms the multi-agent system, Bedrock and Strands orchestration, but never says Strands is open source or comes from AWS, so the claim adds detail.
