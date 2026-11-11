# Prove with proof controls only

Branch: `prove/proof-control`. Base: `prove/formalqualbench-evals` at
`b45e6ff2ad0e8a177370850014c08f805dd18989` (the recorded P20 version).

Run `unity prove` as usual. The `unity-prove` MCP server exposes strategy reservations,
candidate submission, mechanical integration, synchronization, proof status, and diagnostic
artifact reads. The discussion server and its shell aliases are disabled for Prove. There are
no discussion, question, shared-finding, ledger, peer-assistance, or peer-endorsement tools.
Generated prompts and briefs contain only the proof controls and task context.

The roster, reasoning-effort settings, worker worktrees, target contracts, candidate verifier,
merge lock, critic decisions, completion requirements, optional prover services, and retrospective
phase retain the P20 behavior. Question-triggered wakeups are removed. Internal proof state
continues to use `.unity/forum/prove-state.json`; that storage path does not expose a discussion
interface. Use a fresh benchmark workspace when comparing variants.

This branch is an evaluation variant. Its offline tests check tool exposure, prompt content,
claim and candidate gates, verification outcomes, critic retries, and workspace preservation.
Benchmark success rates must be measured separately.
