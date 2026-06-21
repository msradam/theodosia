---
title: 'ITBench SRE: raw agent vs gated, a controlled study'
description: "A controlled study on IBM Research's ITBench SRE benchmark: one fixed agent run raw versus gated through a Theodosia FSM, scored by the benchmark's own judge. Gating significantly improved root-cause precision."
---

Theodosia's headline property is structural and holds regardless of any
accuracy number: every step an agent takes, and every step it was refused,
lands in a typed, replayable, hash-chained ledger you can read and verify
after the fact. The study below is supporting evidence, not the headline.

The result rests on [ITBench](https://github.com/itbench-hub/ITBench)'s own
parts: the agent it ships, the scenarios it ships, and its own judge. We
changed one thing, the gate.

## Setup

We ran one fixed agent (`claude-haiku-4-5`) two ways over all 35
[ITBench-Lite](https://huggingface.co/datasets/ibm-research/ITBench-Lite) SRE
scenarios, three trials each.

- **Tier 1 (raw):** the prompted agent with ITBench's tools exposed directly.
- **Tier 2 (gated):** the same agent, same prompt, same tools, same data, its
  procedure mounted as a Theodosia-gated FSM.

ITBench's own `itbench_evaluations` LLM judge (`claude-sonnet-4-6`) scored both
arms, run unmodified. Both arms completed all 105 runs.

## Result

| Metric (ITBench judge, n=35) | Raw | Gated | Delta |
|---|---|---|---|
| Root-cause entity F1 | 0.408 | 0.561 | **+0.152** |
| Entity precision | 0.369 | 0.525 | +0.156 |
| Entity recall | 0.514 | 0.648 | +0.133 |
| Propagation chain | 0.558 | 0.591 | +0.032 |
| Root-cause reasoning | 0.464 | 0.559 | +0.095 |
| Fault localization | 0.829 | 0.848 | +0.019 |

The entity F1 gain is significant (paired Wilcoxon p=0.0097, paired t
p=0.0038) and robust. It survives dropping the single largest scenario
(p=0.017), the bootstrap 95% CI on the delta is [+0.06, +0.25], and at the
trial level the gated arm wins 40 to 16.

## The mechanism: less noise in the conclusions

Precision drives the gain. The gate makes the agent blame control-plane noise
far less: the raw agent marks a `kube-system` or `cluster-wide` entity as a
contributing factor in 48% of runs, the gated agent in 31%. That count is a
direct measure of noise in the agent's output, independent of any scoring
choice.

This is structure, not a smarter model. Forcing the agent to state a
hypothesis and its evidence before it may conclude stops it from blaming what
it never investigated.

## Why this is credible: seven confounds, found and fixed first

The trust signal here is not the number but what we had to control before
believing it. We found seven plausible confounds. Each one could flip the sign
of the result until we made the comparison fair:

1. **A custom scorer.** Swapped in for ITBench's judge as a cross-check.
2. **Missing data.** Scenarios where one arm produced no output.
3. **An untyped tool surface.** Whether typed inputs alone drove the gain.
4. **Observation truncation.** Long tool outputs cut differently per arm.
5. **Phase-order gating.** Whether ordering alone, not the gate, drove it.
6. **A paraphrased prompt.** ITBench's verbatim prompt versus a reworded one.
7. **Reasoning extraction.** How the scorer parsed the agent's rationale.

The reported delta is what survives all seven. The report diagnoses each, with
code.

## Scope

:::caution[Preliminary, and scoped on purpose]
- One agent model, one judge, 35 scenarios. This is preliminary.
- The absolute scores are low for both arms (roughly 0.4 to 0.56) because
  ITBench's own verbatim prompt is noisy. The story is the **delta**, not the
  absolute. Read it relatively.
- The recall half of the win depends on the judge's semantic name matching. A
  strict deterministic matcher that requires the full `namespace/Kind/name`
  ties the two arms on root-cause hit. So the defensible claim is the
  **precision** and **overall F1** effect, not better localization on its own.
:::

The claim, scoped to what holds: gating the procedure significantly improved
root-cause precision over the same raw agent, by suppressing noise in the
agent's conclusions, on a mid-tier model, scored by the benchmark's own judge.

## Reproduction

Every number, the FSM diagram, the seven validity controls in full, and the
reproduction steps live in
[`theodosia-bench/RESULTS.md`](https://github.com/msradam/theodosia-bench/blob/main/RESULTS.md).
