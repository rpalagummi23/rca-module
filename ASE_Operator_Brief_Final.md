# Internal Search Assistant — ASE Operator Brief

**Prepared by:** [Your Name], AI Success Engineer, OpenAI
**Audience:** CIO, CTO, and Senior Technical Leadership
**Scope:** 14-day production review (June 1–14, 2024) · 10% user sample (1,200 requests) · 5 telemetry files

---

> **Bottom line:** This system has two independent problems. First, something is adding ~86 tokens/day to every prompt, doubling API costs in two weeks. Second, the inventory service degraded overnight on June 9, tripling latency on half of all requests and introducing new errors. Neither is self-healing. Both must be resolved before expanding to three new teams.

---

## 1. What Is Happening Today

### 1.1 System and Adoption State

The assistant serves approximately **860 requests/day** from **~750 stores** via a widget in the OneStore employee app. The pipeline is strictly sequential — confirmed by timestamp ordering across all 587 multi-span traces:

**Vector Store Retrieval (70ms) → Inventory Lookup (200–550ms, if needed) → GPT-4o (750ms)**

Traffic splits into three routes:

| Route | % of Traffic | What It Does | Tool Calls |
|-------|-------------|--------------|------------|
| `policy_only` | 49.1% (589 requests) | Retrieval + GPT-4o only | 87.4% have zero tool calls; 12.6% unexpectedly do |
| `mixed` | 26.8% (321) | Retrieval + Inventory + GPT-4o | 100% use tool calls (1–2 per request) |
| `inventory` | 24.2% (290) | Retrieval + Inventory + GPT-4o | 100% use tool calls (1–2 per request) |

Volume is **completely flat** — no adoption growth visible in 14 days. Each sampled user made exactly one request, suggesting broad but shallow usage for ad-hoc lookups rather than extended sessions. **This is a critical adoption signal: 1.0 queries per user over 14 days means employees try the tool once and never return.** Traffic peaks at **11–13h** (lunch rush) and **16–18h** (end of shift), consistent with retail store patterns. Weekend volume is the same as weekday.

Caching exists but is barely effective:
- **Policy/retrieval cache:** 5.8% hit rate (70/1,200)
- **Inventory cache:** 5.6% hit rate among inventory-route requests (34/611)
- **OpenAI response cache:** 0.0% hit rate (0/1,200)
- **91.4% of requests hit neither cache**

No dedicated caching infrastructure (Redis, ElastiCache) is visible in the architecture. Caching likely lives in ephemeral Lambda memory — cold starts (2.5% of requests) and multiple concurrent Lambda instances mean most invocations start with empty caches.

### 1.2 Current Architecture and Request Flow

```
┌──────────────┐     ┌──────────────┐     ┌────────────────────────────────────────┐
│   Employee   │     │     API      │     │       Lambda (Node.js)                 │
│   OneStore   │────▶│   Gateway    │────▶│                                        │
│   Widget     │◀────│   (REST)     │◀────│   ┌──────────┐  SERIAL  ┌───────────┐ │
└──────────────┘     └──────────────┘     │   │ Step 1:  │────────▶│ Step 2:   │ │
                                          │   │ Retrieval│         │ Inventory │ │
  No streaming       Synchronous only     │   │ (70ms)   │         │ (200-550ms│ │
  No feedback        No SSE/WebSocket     │   └──────────┘         └───────────┘ │
  No citations       Buffers full resp.   │         │    waits for both    │      │
  Generic errors                          │         ▼                      ▼      │
                                          │   ┌──────────────────────────────────┐│
                                          │   │ Step 3: OpenAI GPT-4o (750ms)   ││
                                          │   │ - No streaming (stream: false)   ││
                                          │   │ - No model routing (GPT-4o all) ││
                                          │   │ - No prompt caching structure    ││
                                          │   │ - Naive retry (no backoff)       ││
                                          │   │ - No error classification        ││
                                          │   └──────────────────────────────────┘│
                                          │                                        │
                                          │   ✗ No circuit breakers                │
                                          │   ✗ No graceful degradation            │
                                          │   ✗ No provisioned concurrency         │
                                          │   ✗ Serial calls (retrieval→inv→OAI)   │
                                          │                                        │
                                          │   CloudWatch ◀─── logging only         │
                                          │   ✗ No dashboard  ✗ No alerting        │
                                          │   ✗ No prompt/completion logging       │
                                          └────────────────────────────────────────┘
```

**Key architectural finding:** The Lambda executes all downstream calls **strictly in serial**. I examined all 611 traces that include both retrieval and inventory spans. **Every single one** — 611 out of 611 — shows retrieval completing before inventory starts, despite having zero dependency on each other:

```
  Actual trace (req_5048bf7b539b):
  ─────────────────────────────────────────────────────────────
  Retrieval:  ██                       40ms   (ends ~:40.100)
  Inventory:      ██████               143ms  (starts :40.106 — waits for retrieval)
  OpenAI:              ██████████████████████  554ms  (starts :40.253)
  ─────────────────────────────────────────────────────────────
  Total: 793ms   |   Wasted: 40–70ms waiting on retrieval before inventory
```

A simple `Promise.all([retrieval(), inventoryLookup()])` would overlap these calls, saving **70–140ms on 51% of traffic** with minimal code change.

---

### 1.3 Problem 1: Prompt Token Accumulation (Drives Cost)

Average prompt tokens per request grew from **871 to 1,981 over 14 days (+127%)** — a near-perfect linear increase of **85.89 tokens/day** (R² = 0.9997).

| Day | Avg Prompt Tokens | Avg Cost/Request (sample) |
|-----|-------------------|--------------------------|
| Jun 1 | 871 | $11.0 |
| Jun 7 | 1,379 | $17.1 |
| Jun 14 | 1,981 | $24.3 |

**Why we know this is a system prompt issue (not user behavior, not retrieval, not route mix):**

- **Growth is identical across all three routes:** policy_only +85/day, inventory +86/day, mixed +88/day. Since `policy_only` doesn't call the inventory service at all, the growth must be in shared prompt infrastructure.
- **Retrieval payload is flat** at ~1,155 bytes/day with zero trend (R² = 0.02). The vector store is not returning more content.
- **Zero correlation** between retrieval payload size and prompt tokens (r = -0.02).
- **Every user is unique** — 1,200 requests from 1,200 distinct user hashes, each appearing exactly once. This rules out conversation history accumulation.
- **Growth is uniform within each day** — coefficient of variation is ~5%. Not driven by outliers.
- **Route mix proportions fluctuate but don't trend.** Can't explain the growth via shift toward higher-token routes.

**Completion tokens are flat** at ~215/day (slope: -0.01 tokens/day). The model generates the same length answers — it just reads 2x more input.

**100% of cost growth traces to input token inflation.** The cost formula in the data is exactly `prompt_tokens × $0.012 + completion_tokens × $0.0024` (verified with zero prediction error across all 1,200 records). Under this formula, $1,121 of the $1,122 total cost increase comes from input tokens.

The growth likely started **before our observation window** — day 1 already shows ~320 tokens above what a minimal RAG prompt would require (~550 tokens), suggesting accumulation began roughly 4 days prior.

**Additionally, prompt token variance is high** (stdev 354, range 669–2,100). This variance prevents OpenAI's automatic prompt caching from activating consistently and makes cost and latency unpredictable. A chunk budget (see Action 7) would reduce this variance alongside fixing the growth.

**Most likely causes:** An automated process (cron job, daily deployment, content sync) appending few-shot examples, knowledge base metadata, or behavioral instructions to the system prompt.

---

### 1.4 Problem 2: Inventory Service Degradation (Drives Latency and New Errors)

Between June 8–9, the inventory service experienced a sudden performance degradation:

| Metric | Jun 1–8 | Jun 9–14 | Change |
|--------|---------|----------|--------|
| Mean latency (OK requests) | 197ms | 520ms | **+164% (2.6×)** |
| p95 latency | 313ms | 897ms | **+187%** |
| Timeout setting | 900ms | 1,300ms | **Raised overnight** |
| Errors | 0 | 14 | **New failure mode** |
| Retry rate | 3.5% (12/339) | 12.1% (33/272) | **+246%** |

**This was not caused by increased load.** Inventory-route request volume is flat (43/day week 1 vs 45/day week 2 in the sample). Something changed in the service itself.

Someone raised the timeout from 900ms to 1,300ms on June 9 at 00:56 UTC — a reactive band-aid. Despite the raised ceiling:
- **9 requests still timed out** (HTTP 504, durations 1,231–1,278ms)
- **5 requests returned HTTP 500** server errors — a new failure mode starting June 11, indicating the service is now actively breaking, not just slow

**Policy-only requests (49% of traffic) experienced zero latency change** — confirming inventory is the sole cause of the system-wide latency increase:

| Component | policy_only W1 | policy_only W2 | inv/mixed W1 | inv/mixed W2 |
|-----------|---------------|---------------|-------------|-------------|
| Retrieval | 72ms | 69ms | 72ms | 67ms |
| Inventory | — | — | 198ms | 470ms |
| OpenAI | 757ms | 749ms | 745ms | 723ms |
| Lambda overhead | 76ms | 75ms | 70ms | 70ms |
| **Total** | **905ms** | **894ms** | **1,086ms** | **1,330ms** |

OpenAI API latency is **flat at ~750ms** with zero correlation to prompt tokens (r = -0.04). Lambda overhead is constant at ~73ms. The only degrading component is inventory.

**These two problems are provably independent.** Prompt token growth is continuous across the June 8/9 boundary (1,459 → 1,553, same ~86 token/day slope, no step change). Inventory latency shows a clear overnight step change (186ms → 565ms). A single deployment could not cause both a gradual linear trend and a sudden step function. They have different root causes and require different fixes.

**SLA impact of inventory degradation:** Before June 9, 55.7% of all requests completed in under 1 second. After June 9, only 35.4% did — a **20-percentage-point drop** in sub-second response rate, entirely caused by the inventory service.

---

### 1.5 Error Analysis

**33 total errors** over 14 days (2.8% overall). **100% occur during peak hours (11–13h, 16–18h). Off-peak error rate is 0.0%.** Peak-hour error rate is **8.2%** — meaning 1 in 12 employee requests fails during the busiest periods.

There is a **bimodal concurrency distribution** with a gap from 121–180 (zero requests observed). Off-peak concurrency maxes at 120; peak starts at 181. This means the system has **two discrete operating modes** with no gradual transition — an important infrastructure planning insight for scaling.

**Important correction:** Within peak hours, the error rate is roughly flat across concurrency levels (6–13% at every concurrency bucket from 180 to 420). The real predictor of errors is **peak vs off-peak**, not the exact concurrency level. This means scaling the system doesn't just require handling more concurrency — it requires understanding why peak windows inherently cause failures.

**Hourly traffic and error distribution:**

| Hour Band (UTC) | Requests | Errors | Avg Latency |
|----------------|----------|--------|-------------|
| 06–10 | 235 | 0 | 1,001ms |
| **11–13** | **298** | **19** | **1,196ms** |
| 14–15 | 133 | 0 | 920ms |
| **16–18** | **309** | **14** | **1,189ms** |
| 19–23 | 181 | 0 | 945ms |
| 00–05 | 42 | 0 | 924ms |

These 33 errors split cleanly into two independent failure sources:

| Error Source | Week 1 | Week 2 | Root Cause |
|-------------|--------|--------|------------|
| OpenAI/Lambda layer | 10 | 9 (stable) | Rate limits / 429 (6), API errors / 502 (6), Lambda overload / 504 (7) |
| Inventory service | 0 | 14 (NEW) | Timeouts / 504 (9) and server errors / 500 (5) |
| **Total** | **10** | **23** | **+130% week-over-week** |

**Key insight: OpenAI-layer errors barely changed (10 → 9).** The entire error increase is from the inventory service. This means the OpenAI API integration itself is stable — the problem is entirely on the customer's infrastructure side.

**Inventory errors are accelerating:**
- Jun 9–10: 2 errors/day average
- Jun 11–12: 2.5 errors/day average
- Jun 13: **8 errors in a single day** (worst day in the dataset)
- Jun 14: 1 error (but HTTP 500s continue)

This is not a steady-state degradation — it's worsening. Without intervention, error rates will continue to climb.

**Critical finding:** On all 14 inventory failures, GPT-4o still generated a successful response (`status=ok` in openai_usage) that was **discarded** because the downstream tool call failed. The company paid for 14 wasted API completions with no value delivered to the employee. **This is the strongest argument for graceful degradation**: when inventory fails but retrieval and OpenAI succeed, serve the policy-based answer with a caveat ("Inventory data is temporarily unavailable — here's the policy information") instead of returning a raw 500 error.

**Errors never cascade across spans.** When inventory fails, OpenAI and retrieval are fine. When OpenAI fails, retrieval and inventory are fine. Retrieval has **zero failures** (0/1,200). This indicates decent architectural isolation.

---

### 1.6 Response Quality

| Finish Reason | Count | % | Meaning |
|--------------|-------|---|---------|
| `stop` | 1,019 | 84.9% | Clean, complete answer |
| `tool` | 100 | 8.3% | Model requested a tool call (97 succeeded, 3 failed) |
| `length` | 62 | 5.2% | **Truncated** — answer cut off mid-response |
| `error` | 19 | 1.6% | API failure — no answer at all |

**5.2% of employees receive cut-off answers.** Truncated completions range from 154–273 tokens, while successful completions range from 142–288. The mean completion is only 214 tokens, suggesting the `max_tokens` limit is set to approximately 300–500. **This is a one-line fix:** raise `max_tokens` to 1,024. You pay only for tokens actually generated, not the ceiling — so cost impact is negligible. This immediately eliminates truncated answers for 62 employees in the sample (projected ~620 over the full traffic).

By route, `policy_only` has the highest clean rate (92%), while `inventory` and `mixed` are lower (78–79%) due to tool-call overhead and higher inventory-error exposure.

**Unexpected tool calls on policy_only:** 74 out of 589 policy_only requests (12.6%) include tool calls, yet **zero of them** triggered an actual inventory span. The model is either hallucinating tool calls or tool/function definitions are being sent to GPT-4o on all routes unnecessarily — wasting prompt tokens.

---

### 1.7 Retry Behavior

Retries reveal different failure characteristics by service:

| Service | Retried Requests | Success After Retry | Failure After Retry |
|---------|-----------------|-------------------|-------------------|
| OpenAI chat | 58 (4.8%) | 43 (74.1%) | 15 (25.9%) |
| Inventory | 45 (7.4%) | 43 (95.6%) | 2 (4.4%) |
| Retrieval | 0 (0%) | N/A | N/A |

**Inventory retries almost always succeed** (95.6%) — indicating transient latency spikes, not persistent failure. This supports the circuit breaker approach: a brief retry + fallback pattern would recover most failures.

**OpenAI retries fail 26% of the time** — mainly because 429 rate limiting persists through retries (it's a sustained capacity issue, not a transient blip). Each retry adds ~360ms of latency overhead.

**Inventory retry rate jumped in Week 2:** 3.5% (W1) → 12.1% (W2), reflecting the service degradation. This is another signal the situation is worsening.

**Current retry strategy is naive:** all errors are retried identically regardless of type. A 400 (bad request) is retried the same as a 429 (rate limit) or 500 (server error). Proper error classification would: retry 429s (respecting `Retry-After` headers), retry 500/502/503 with exponential backoff, and immediately fail through to graceful degradation on 400/401/403. This saves retry latency on errors that will never succeed and reduces peak congestion from unnecessary retry storms.

---

### 1.8 Lambda Cold Starts

| Metric | Cold Start | Warm Start |
|--------|-----------|-----------|
| Frequency | 2.5% (30/1,200) | 97.5% |
| Avg latency | 1,293ms | 1,073ms |
| Latency penalty | +220ms | Baseline |
| Error rate | **0.0%** | 2.8% |

Cold starts add ~220ms but cause **zero errors**. They occur exclusively during off-peak (when Lambda scales down), so they don't overlap with the error-causing peak periods. Cold starts are not a priority concern, but they do contribute to the poor cache hit rates — each cold-started Lambda instance begins with empty in-memory caches.

---

### 1.9 Adoption Risk

**The system has a try-once-and-abandon pattern.** 1,200 unique users made 1,200 requests — exactly 1.0 queries per user over 14 days. The maximum requests from any single store is 3. Employees try the tool once and never return.

The root cause is unknown and could be any combination of: poor answer quality (invisible without feedback data), slow response times, truncated answers, lack of trust (no source citations), poor UX, or lack of discoverability. **We cannot diagnose this without a feedback mechanism** — which is why adding thumbs-up/down feedback (Section 2.2, Action 3) and prompt/completion logging (Section 2.2, Action 4) are prerequisites for understanding and improving adoption.

For an executive audience: an AI assistant with flat adoption and 1.0 queries/user is not delivering its intended value, regardless of technical metrics. Expanding to 3 new teams on this foundation risks replicating the same low-adoption pattern at 4× scale.

---

### 1.10 Additional Context

**Weekend vs weekday:** Traffic volume is identical 7 days/week (~86 requests/day in sample). Saturday latency is slightly lower (995ms avg vs ~1,100ms weekday) due to lower concurrency, but the system is used consistently throughout the week — reflecting retail operations.

**Cost composition:** Overall, 97.1% of total cost comes from input tokens, 2.9% from output tokens. Under the data's cost formula, input tokens are 5× more expensive per token than output tokens. This means every optimization should focus on reducing input tokens (prompt compression, caching, fine-tuning) rather than output tokens.

**Data timeline note:** This telemetry is from June 2024. OpenAI's automatic prompt caching launched in October 2024. The 0% OpenAI cache hit rate in the `spans_openai_chat.cache_hit` field reflects a different (pre-caching) mechanism, not a misconfiguration. Prompt caching is available now and should be implemented as described in Section 2.2.

---

### 1.11 Biggest Risks

**1. Runaway cost.** If prompt growth continues unchecked, cost per request doubles every ~14 days. At current trajectory with 4× expansion volume, projected monthly cost becomes unsustainable within one quarter.

**2. Scaling on a broken foundation.** Peak error rate is already 8.2%. The inventory service is degrading further (HTTP 500s started June 11). Adding 3 teams without fixes means more users experiencing worse reliability.

**3. Employee trust erosion.** 6.8% of requests return incomplete or failed answers. In retail, employees who lose trust in the tool stop using it — and the 1.0 queries/user data suggests this is already happening. Recovery is harder than prevention.

**4. No graceful degradation.** When any downstream service fails, the entire request fails. 14 requests paid for a successful GPT-4o completion that was discarded. No fallback to serve partial answers.

**5. Poor cache utilization.** 91.4% cache miss rate means the system re-computes everything from scratch nearly every time. Common policy questions across hundreds of stores are fully reprocessed each time.

**6. No quality visibility.** There is no feedback mechanism, no prompt/completion logging, no evaluation pipeline, and no dashboard. The team cannot tell whether answers are correct, whether employees find them useful, or whether changes help or hurt. Flying blind on the most important metric.

---

## 1.12 Key Questions I Need Answered

Before finalizing any recommendations, I need to validate my understanding:

1. **What is the system prompt template, and is anything appending to it automatically?** This is the single most important question — the root cause of the cost spiral depends on the answer.
2. **What changed with the inventory service on June 8–9?** Deployment, migration, vendor update?
3. **Who raised the inventory timeout to 1,300ms and was this intended as temporary?**
4. **When inventory fails, what does the employee see?** An error message? Or the GPT-4o response without inventory data?
5. **What is the current `max_tokens` parameter on OpenAI API calls?**
6. **What is the current OpenAI usage tier?** (Determines rate limits — see Section 2.3 below.)
7. **What query patterns will the 3 new teams introduce?** Same as current, or different use cases?
8. **Are tool/function definitions sent to OpenAI for all routes, including policy_only?**
9. **What is the AWS Lambda concurrency limit, and is reserved concurrency configured?**
10. **Is there monitoring and alerting in place?** Was anyone alerted to the Week 2 degradation?

### 1.13 Documented Assumptions

In the absence of answers, I proceed with:

1. The 10% sample is representative (uniform random sampling across users/stores).
2. The cost field reflects the enterprise's internal cost model (including infrastructure overhead); absolute values may differ from raw API pricing, but growth rates are valid.
3. Prompt token growth is caused by system-level context injection, not user behavior (proven: every user appears once, growth is uniform across all dimensions).
4. Inventory degradation is a service-side issue, not load-driven (proven: request volume is flat).
5. Expansion to 3 new teams means ~4× total volume (could range 2×–5× depending on team sizes).
6. New teams will have broadly similar query patterns to current users.
7. Peak hours (11–13h, 16–18h) correspond to retail shift patterns and will persist.
8. No streaming is used (supported by ~5ms delta between usage latency and span duration).
9. No dedicated caching infrastructure exists outside Lambda memory.
10. The 14 wasted OpenAI calls represent actual API spend with no value delivered.

---

## 2. What I Would Do Next

### 2.1 Phase 1 — Stabilize (Weeks 1–2)

**Action 1: Diagnose and halt prompt token growth (top priority).**

I would work directly with your engineering team to review the system prompt template and any automated injection pipelines. We identify what adds ~86 tokens/day, cap it, and target a stable prompt under 1,000 tokens. This immediately halts cost acceleration.

Specific things to look for:
- A daily cron job or deployment pipeline that appends few-shot examples, knowledge base summaries, or behavioral instructions
- A logging mechanism that injects prior interaction context into the prompt
- Growing tool/function definition schemas
- An expanding list of retrieved document metadata being injected outside the retrieval span

**Action 2: Investigate and remediate the inventory service.**

Your infrastructure team needs to determine what changed June 8–9. In parallel, I recommend implementing **graceful degradation with explicit partial answers**: when inventory fails but retrieval and OpenAI succeed, serve the policy-based answer with a caveat: *"I don't have live inventory data right now, but here's the policy information. For current stock levels, check [alternative system]."* When OpenAI itself fails, return a static fallback directing the employee to alternative resources. **Never return a raw 500/502/504 error to the employee.**

This converts errors into degraded-but-useful answers, stops wasting successful API completions (14 wasted in the sample), and protects against cascading failures during the 3-team expansion.

The circuit breaker pattern complements this: after N consecutive inventory failures in a time window, skip the call entirely and fall through to the partial-answer handler immediately. This prevents timeout-induced latency inflation (waiting 900–1,300ms for a call you know will fail).

**Action 3: Implement user feedback mechanism.**

Add thumbs-up/down buttons after every response in the OneStore widget, linked to `trace_id`. Log feedback to a durable store (DynamoDB or equivalent). This is the minimum viable signal for answer quality and the prerequisite for diagnosing the 1.0 queries/user adoption problem.

**Action 4: Enable prompt/completion logging.**

Log full prompt + completion for 10% of requests to a restricted S3 bucket, linked to `trace_id`. Privacy-review the logging scope with security/compliance before enabling. This data is required for:
- The retrieval quality audit (do retrieved chunks actually answer the question?)
- Building the evaluation pipeline (golden Q&A set)
- Debugging prompt token growth (what's actually in the prompt?)

**Action 5: Quick configuration fixes.**

Three one-line fixes with immediate impact:
- **Raise `max_tokens` to 1,024**: Eliminates 5.2% truncated responses. Cost impact: negligible (you pay per token generated, not the ceiling).
- **Remove tool/function definitions from policy_only requests**: Saves ~73 tokens/request on 49% of traffic and eliminates 74 spurious tool calls.
- **Review and confirm OpenAI usage tier**: Check Settings → Limits on the OpenAI dashboard. If Tier 1, the 6 throttle events may resolve by reaching Tier 2 (typically happens automatically with $50+ in spend). Request increase if needed.

**Action 6: Set up monitoring and alerting.**

If not already in place, configure alerts for:
- Inventory service p95 latency > 500ms
- Error rate > 3% in any rolling hour
- Daily prompt token average exceeding a threshold (e.g., +10% over baseline)
- OpenAI API 429 (rate limit) events

The Week 2 degradation should have been caught within hours. The fact that someone raised the inventory timeout manually suggests there's at least some monitoring, but no automated alerting.

---

### 2.2 Phase 2 — Optimize (Weeks 3–6)

**Action 7: Parallelize retrieval and inventory calls.**

Replace the serial `await retrieval(); await inventoryLookup()` pattern with `const [chunks, productData] = await Promise.all([retrieval(), inventoryLookup()])`. This saves **70–140ms on 51% of traffic** (every request that hits the `inventory` or `mixed` route). Low effort, high impact, zero risk — these calls have no dependency on each other.

**Action 8: Implement retrieval chunk budget.**

Cap retrieved context at a fixed budget (e.g., top 3 chunks or ~800 tokens). Current prompt tokens range from 669 to 2,100 (stdev 354). A chunk budget:
- Makes prompt size predictable, improving cost forecasting
- Enables OpenAI prompt caching (consistent prefix → higher cache hit rate)
- Reduces the baseline prompt size, not just the growth rate
- Should be tuned after the retrieval quality audit (Action 4 enables this)

**Action 9: Enable OpenAI Prompt Caching.**

This is arguably the highest-value OpenAI platform feature for this use case. Here's exactly how it works:

- **Automatic:** OpenAI's prompt caching activates automatically on all API requests for GPT-4o and GPT-4o-mini. No code changes, no opt-in required. (Launched October 1, 2024.)
- **How it works:** The API caches the longest prefix of a prompt that has been previously computed, starting at 1,024 tokens and increasing in 128-token increments. If the next request shares the same prefix, the cached portion is reused.
- **Discount:** Cached input tokens receive a **50% discount** and process **up to 80% faster** (reduced time-to-first-token).
- **Eviction:** Caches are typically cleared after 5–10 minutes of inactivity, up to 1 hour during off-peak.
- **What's cacheable:** The entire request prefix — messages, images, audio, tool definitions, and structured output schemas.
- **Key requirement:** The static content (system prompt, tool definitions) must come **at the beginning** of the prompt, with variable content (retrieval results, user query) at the end. Prefix matching is exact — any difference in the prefix breaks the cache.

**Current state:** 0% cache hit rate. With the system prompt at ~1,700+ tokens (well above the 1,024 minimum), caching should be automatic — but **only if the prompt is structured with the static prefix first**. If retrieval context or user-specific content is injected before the system prompt, every request has a different prefix and nothing caches.

**My recommendation:** I would review the prompt structure with your engineering team and advise on reordering to maximize cache hits. This is a zero-code-change optimization on the API call side — just prompt restructuring. The `cached_tokens` field in the API response's `usage.prompt_tokens_details` will confirm whether caching is working.

**For advanced optimization:** OpenAI also offers `prompt_cache_key` — an optional parameter that influences routing to improve cache hit rates when many requests share long common prefixes. And `extended` cache retention policy, which aims to retain caches for up to 24 hours instead of the default 5–10 minutes. Both would be beneficial for this high-traffic use case.

**Action 10: Evaluate model routing (GPT-4o vs GPT-4o-mini).**

Policy-only queries (49% of traffic) handle relatively straightforward policy Q&A. GPT-4o-mini may be sufficient for these at a fraction of the cost:

| Model | Input Price | Output Price | Context Window |
|-------|-----------|-------------|----------------|
| GPT-4o | $2.50/M tokens | $10.00/M tokens | 128K |
| GPT-4o-mini | $0.15/M tokens | $0.60/M tokens | 128K |

GPT-4o-mini is **~94% cheaper on input** and **94% cheaper on output** than GPT-4o. For the 49% of traffic that's policy-only, this represents a massive cost lever.

**But quality must be validated first.** I would help your team run a structured evaluation:
- Select 200 representative policy questions (spanning common and edge-case queries)
- Run them through both GPT-4o and GPT-4o-mini with identical prompts
- Score for accuracy, completeness, and relevance using domain experts or LLM-as-judge
- If quality difference is <5%, route policy-only traffic to GPT-4o-mini
- If quality drops >10%, keep GPT-4o for all routes

This is where OpenAI's **Evals framework** comes in (see Action 15 in Section 2.3 below).

**Action 11: Consider fine-tuning to compress prompt size.**

If the prompt growth is from accumulated few-shot examples or behavioral instructions, **fine-tuning GPT-4o** can encode that behavior directly into the model weights — potentially eliminating 1,500+ tokens of prompt context entirely.

OpenAI's fine-tuning for GPT-4o:
- **Training cost:** $25/M tokens
- **Inference:** $3.75/M input, $15/M output
- **Minimum recommended:** 50–100 well-crafted training examples
- **Key benefit for this use case:** If the system prompt contains extensive formatting instructions, domain-specific tone guidance, or behavioral rules, fine-tuning lets you remove these from the prompt while maintaining (or improving) output consistency
- **Prompt caching still works** on fine-tuned models — so you get both benefits

This is a medium-term optimization. I would connect your team with OpenAI's fine-tuning resources and guide training data preparation.

**Action 12: Optimize function calling.**

Several quick wins:
- **Remove tool definitions from policy_only requests.** If tool schemas are being sent on all routes, removing them from policy_only saves ~73 tokens/request and eliminates the 74 spurious tool calls.
- **Use Structured Outputs for inventory responses.** OpenAI's Structured Outputs (available on GPT-4o-2024-08-06 and later) guarantee the model returns valid JSON matching a specified schema — 100% reliability on schema adherence vs <40% for older models. Enable with a single parameter: `strict: true` on the function definition.
- **Best practice from OpenAI docs:** "Explicitly describe the purpose of the function and each parameter. Use the system prompt to describe when (and when not) to use each function." This directly addresses the 74 unnecessary tool calls on policy_only.

**Action 13: Add shared caching infrastructure and implement component-level caching.**

An external cache layer (Redis/ElastiCache) for retrieval results and inventory lookups would materially reduce both latency and redundant API calls. The current exact-match query string caching is architecturally wrong for free-form natural language queries — "Return policy for TVs" and "Can I return a television?" never match despite being the same question.

- **Retrieval cache:** Key on embedding similarity (cosine ≥ 0.95). After computing the query embedding, check if a recent query with high similarity exists. If so, reuse cached chunks. TTL: 6–24 hours. Expected hit rate: 20–40%.
- **Inventory cache:** Key on `product_id + store_id` — structured, deterministic keys extracted from the query before the API call. TTL: 10 minutes. Same product/store combo asked by different employees reuses cached data. Expected hit rate: 30–50%.

**Action 14: Implement proper retry strategy with error classification.**

Replace the current naive "retry once immediately" approach:
- **Classify errors:** 429 → retryable (respect `Retry-After` header). 500/502/503 → retryable with exponential backoff (200ms → 400ms → 800ms, max 3 attempts, with jitter). 400/401/403 → not retryable (log, surface to monitoring, fall through to graceful degradation immediately).
- **Circuit breaker per downstream service:** After N failures in a rolling window, open the circuit — skip the call entirely and fall through to graceful degradation. Prevents retry storms during peak congestion.
- This combination reduces unnecessary retry latency, prevents the positive feedback loop where retries amplify peak congestion, and ensures non-retryable errors fail fast.

---

### 2.3 Phase 3 — Expand (Weeks 7–12)

**Action 15: Build an evaluation framework before any model/prompt changes.**

OpenAI provides two complementary eval tools:

1. **OpenAI Evals API** (hosted): Create and run evals programmatically or through the OpenAI dashboard. Supports:
   - **Basic evals:** Deterministic comparison of model output to known-correct answers (string match, JSON parsing)
   - **Model-graded evals:** Use a separate model (e.g., GPT-4o) to judge the quality of another model's output
   - **Stored completions:** Test for regressions when prompts change
   - **Continuous evaluation:** Run evals on every change, monitor for drift

2. **OpenAI Evals framework** (open-source, GitHub): 17,600+ stars, community-maintained registry of benchmarks. Good for custom evaluations specific to your domain.

**For this use case specifically, I would recommend:**
- Build a **golden dataset** of 200+ representative questions across all three routes (policy, inventory, mixed) with domain-expert-validated answers
- Define metrics: **answer accuracy** (does it answer the question correctly?), **completeness** (did it include all necessary information?), **faithfulness** (did it hallucinate or contradict the source documents?), **format compliance** (is it well-structured for the employee?)
- Use **model-graded evals with GPT-4o as judge** for scalable scoring, validated against human ratings
- Run this eval suite **before every change**: prompt modifications, model routing switches, fine-tuning deployments
- This makes quality **visible and measurable** rather than anecdotal

**Action 16: Implement streaming end-to-end.**

Currently, employees wait mean 1,079ms (P95: 1,669ms) staring at a blank screen before any text appears. Streaming would deliver first tokens in ~100–200ms — a **5–10× improvement in perceived responsiveness**.

Implementation requires changes at three layers:
- **OpenAI API:** Set `stream: true` on the Chat Completions call. Lambda consumes the SSE stream.
- **Gateway:** Switch to Lambda Function URL with response streaming enabled, or use API Gateway WebSocket API. Lambda Function URL is the simplest path — it supports response streaming natively.
- **Widget:** Implement SSE/WebSocket consumer in OneStore widget. Render tokens as they arrive.

Total generation time stays the same, but the experience changes from "wait 1 second for a wall of text" to "text starts appearing almost immediately." This is particularly impactful in a retail setting where employees are at the counter with customers waiting.

**Action 17: Request AWS Lambda concurrency increase.**

Current peak concurrency reaches ~420. At 4× scale, projected peak is ~1,680 — above the **default AWS Lambda limit of 1,000** concurrent executions. Request a limit increase to at least 2,500 before onboarding new teams.

Additionally, configure **provisioned concurrency of 350–400 instances** scheduled for peak hours (9am–6pm) via CloudWatch Events rule. This eliminates `lambda_overload` errors and cold starts during peak hours. Off-peak, on-demand scaling is sufficient.

**Action 18: Verify OpenAI usage tier.**

OpenAI uses a **tier system** (Tier 1 through Tier 5) that automatically increases as your API spend grows. Rate limits are measured in:
- **RPM** (requests per minute)
- **TPM** (tokens per minute — both input and output combined)
- **RPD** (requests per day)

Current estimated peak RPM is ~20–30 (well within Tier 1's 500 RPM for GPT-4o). Current estimated peak TPM is ~1,960 — within Tier 1's 30,000 TPM. At 4× scale with continued prompt growth, estimated peak TPM would reach ~8,000–15,000, still within Tier 1 limits.

However, the 6 existing 429 (throttle) errors suggest **burst spikes** that exceed instantaneous limits even at low average TPM. OpenAI enforces rolling 60-second windows, and rate limits can be **quantized** into per-second sub-windows. A burst of 10 requests in 1 second could trigger throttling even if the per-minute average is low.

**My recommendation:** I would review your account's current tier and rate limits on the OpenAI dashboard (Settings → Limits). If you're on Tier 1, the 6 throttle events may resolve by reaching Tier 2 (which typically happens automatically with $50+ in spend). If you need higher limits faster, OpenAI provides a rate limit increase request form. I can facilitate this internally.

**Action 19: Implement per-team cost attribution.**

Use separate **OpenAI API keys or project tags** per team for cost visibility. OpenAI's rate limits are enforced at the organization level, but projects allow per-team usage tracking and budget controls. Set per-team usage limits to prevent any single team's experimentation from creating runaway costs.

**Action 20: Load test the complete pipeline.**

Before onboarding new teams, run a load test at projected 4× volume during simulated peak hours. Validate that all components (Lambda, inventory service, vector store, OpenAI API) handle the increased concurrency without error rate escalation. Pay particular attention to the bimodal concurrency pattern — the system jumps from <120 to >181 concurrent with no gradual ramp.

I recommend onboarding **one team as a pilot**, monitoring for one week, then expanding to the remaining two.

---

### 2.4 Features I Evaluated But Don't Recommend (and Why)

**OpenAI Batch API:** Provides a 50% cost discount but requires a 24-hour turnaround. Since this is a **real-time employee assistant** where response latency matters, Batch API is not viable for the main request flow. It could be useful for offline tasks like nightly pre-computation of FAQ answers or batch evaluation runs, but not for the core product.

**Assistants API:** OpenAI's Assistants API provides built-in conversation state management, file search, and tool orchestration. For this use case, however, the current Chat Completions approach with manual tool orchestration gives more control over the pipeline, and the assistant's single-turn nature (each user appears once) doesn't benefit from Assistants API's conversation threading. Moving to Assistants would add migration complexity without clear benefit.

---

## 3. Proposed Architecture

The proposed architecture addresses every identified problem while preserving the core pipeline structure. No rewrite is required — every change is targeted and incremental.

```
┌──────────────────┐     ┌────────────────────┐     ┌───────────────────────────────────────────────┐
│  Employee Widget │     │  Lambda Function    │     │        Lambda (Node.js) — Enhanced            │
│  (OneStore)      │     │  URL                │     │                                               │
│                  │     │                     │     │  ┌─────────────────────────────────────────┐  │
│  + Streaming     │◀═══▶│  + Response         │◀═══▶│  │         PARALLEL EXECUTION              │  │
│    display       │ SSE │    Streaming        │     │  │                                         │  │
│  + Thumbs up/    │     │  + WebSocket/SSE    │     │  │  ┌──────────┐      ┌──────────────┐    │  │
│    down feedback │     │    support          │     │  │  │Retrieval │      │  Inventory   │    │  │
│  + Source        │     │                     │     │  │  │ + chunk  │      │ + circuit    │    │  │
│    citations     │     └────────────────────┘     │  │  │   budget │      │   breaker    │    │  │
│  + Friendly      │                                │  │  │ (70ms)   │      │ + graceful   │    │  │
│    error msgs    │     ┌────────────────────┐     │  │  │          │      │   degradation│    │  │
│  + Suggested     │     │  Redis/ElastiCache │     │  │  └────┬─────┘      └──────┬───────┘    │  │
│    queries       │     │                    │     │  │       │   Promise.all()   │            │  │
└──────────────────┘     │  Policy cache:     │     │  │       ▼                   ▼            │  │
                         │   embedding sim    │     │  │  ┌──────────────────────────────────┐  │  │
                         │   ≥0.95 cosine     │     │  │  │ OpenAI GPT-4o / GPT-4o-mini     │  │  │
                         │   TTL: 6-24hr      │     │  │  │ + stream: true                  │  │  │
                         │                    │     │  │  │ + prompt caching (static prefix) │  │  │
                         │  Inventory cache:  │     │  │  │ + model routing by route type   │  │  │
                         │   product+store ID │     │  │  │ + max_tokens: 1024             │  │  │
                         │   TTL: 10min       │     │  │  │ + structured outputs (strict)   │  │  │
                         │                    │     │  │  │ + error classification          │  │  │
                         └────────────────────┘     │  │  │ + exponential backoff + jitter  │  │  │
                                                    │  │  └──────────────────────────────────┘  │  │
                         ┌────────────────────┐     │  └─────────────────────────────────────────┘  │
                         │  Observability      │     │                                               │
                         │                    │     │  + Provisioned concurrency (350-400)           │
                         │  Dashboard:        │◀════│  + Circuit breakers per downstream             │
                         │   adoption, quality│     │  + Graceful degradation (partial answers)      │
                         │   latency, cost,   │     │  + Prompt/completion logging (10% → S3)        │
                         │   errors, cache    │     │  + Per-team cost attribution                   │
                         │                    │     │                                               │
                         │  Alerting:         │     └───────────────────────────────────────────────┘
                         │   error >2%/15min  │
                         │   p95 >2000ms      │     ┌───────────────────────────────────────────────┐
                         │   thumbs-down >40% │     │  Evaluation Pipeline                          │
                         │   any 429 errors   │     │                                               │
                         │                    │     │  Golden Q&A set (200+ pairs)                   │
                         │  Feedback store:   │     │  Model-graded evals (GPT-4o as judge)          │
                         │   trace_id + vote  │     │  Gates all quality-affecting deployments        │
                         └────────────────────┘     │  Continuous regression testing                  │
                                                    └───────────────────────────────────────────────┘
```

**Key changes from current state:**

| Layer | Current | Proposed | Impact |
|-------|---------|----------|--------|
| Widget | Blank screen until full response | Streaming token display, feedback, citations | TTFT: 1,000ms → ~150ms |
| Gateway | REST API Gateway (sync only) | Lambda Function URL with response streaming | Enables streaming pipeline |
| Lambda execution | Serial: retrieval → inventory → OpenAI | Parallel: retrieval ∥ inventory → OpenAI | -70–140ms on 51% of traffic |
| Lambda resilience | All-or-nothing, no circuit breakers | Circuit breakers + graceful degradation | Partial answers instead of 500 errors |
| Lambda capacity | On-demand only, 2.5% cold starts | Provisioned concurrency for peak hours | Eliminates peak overload errors |
| Retrieval cache | Exact-match query string (5.8% hit) | Embedding similarity ≥0.95 (20–40% target) | 3–7× cache improvement |
| Inventory cache | Exact-match query string (5.6% hit) | product_id + store_id keys (30–50% target) | 5–9× cache improvement |
| OpenAI model | GPT-4o for everything | GPT-4o-mini for policy_only (if eval passes) | ~94% cost reduction on 49% of traffic |
| OpenAI streaming | Disabled | `stream: true` | TTFT: 750ms → ~150ms |
| OpenAI prompt | Variable prefix, no caching | Static prefix first, caching active | 50% discount on cached tokens |
| OpenAI retries | Naive retry-once, no classification | Backoff + jitter + error classification | Fewer retry storms, faster failure |
| max_tokens | ~300–500 (5.2% truncation) | 1,024 (negligible cost impact) | Zero truncation |
| Observability | CloudWatch logs only | Dashboard + alerting + feedback + logging | Full visibility into health and quality |
| Quality | No measurement, no evals | Feedback + golden Q&A + eval pipeline | Every change validated before deploy |

---

## 4. How I Would Advise on Tradeoffs

Every optimization involves tradeoffs across **cost, latency, quality, and risk**. I would present these as concrete decision points with data — not abstract concepts:

### Decision 1: Model Routing (Cost vs. Quality)

**Option A:** Switch policy_only traffic (49%) to GPT-4o-mini.
- **Pro:** ~94% cost reduction on those requests. GPT-4o-mini scores 82% on MMLU and excels at straightforward Q&A.
- **Con:** May degrade on complex, nuanced policy questions requiring deep reasoning.
- **Gate:** Run eval suite. Only proceed if quality drop <5%.
- **My recommendation:** Pursue this — it's the biggest single cost lever. But never ship without eval validation.

**Option B:** Keep GPT-4o everywhere, focus only on prompt compression.
- **Pro:** Zero quality risk.
- **Con:** Smaller cost savings (~30–50% from prompt optimization alone).

### Decision 2: Prompt Compression vs. Fine-Tuning (Cost vs. Effort)

**Option A:** Manually trim the prompt — identify and remove the growing content, cap context injection.
- **Pro:** Fast (days), low risk, no training data needed.
- **Con:** Limited — only removes the growth, doesn't optimize the baseline.

**Option B:** Fine-tune GPT-4o to encode behavioral instructions into model weights.
- **Pro:** Eliminates prompt bloat permanently, can improve consistency and tone.
- **Con:** Higher effort (weeks), requires curating 50–100+ training examples, training cost (~$25/M tokens).

**My recommendation:** Fix the immediate growth first (Option A, quick), then evaluate fine-tuning as a medium-term investment (Option B). These are sequential, not exclusive.

### Decision 3: Expand Now vs. Stabilize First (Speed vs. Risk)

**Option A:** Expand immediately to capture business value faster.
- **Pro:** 3 teams get access sooner.
- **Con:** They inherit 8.2% peak failure rate, escalating costs, and degrading inventory service. Unreliable tools lose employee trust fast.

**Option B:** Stabilize for 4–6 weeks, then expand with a pilot.
- **Pro:** New teams launch on solid ground. Trust is preserved.
- **Con:** 4–6 week delay.

**My recommendation:** Stabilize first, pilot with one team at week 7, then full expansion. An unreliable tool that employees abandon is worse than a delayed one they trust. In retail, word-of-mouth among store employees is powerful — a bad first impression will suppress adoption across all three new teams.

### Decision 4: Streaming (Latency Perception vs. Engineering Effort)

**Option A:** Implement streaming end-to-end (widget + gateway + OpenAI).
- **Pro:** TTFT drops from ~1,000ms to ~150ms. Dramatically better perceived experience, especially for retail employees with customers waiting.
- **Con:** Requires changes at three layers (widget, gateway, Lambda). Medium engineering effort.

**Option B:** Keep synchronous, focus on backend optimizations.
- **Pro:** Less engineering work.
- **Con:** Employees still stare at blank screen for 1+ second. Latency improvements from parallelization and caching only save 100–200ms — still perceptible.

**My recommendation:** Implement streaming in Phase 3 after stabilization is complete. It's the single biggest UX improvement available and directly addresses the adoption concern (1.0 queries/user). Backend optimizations should come first, but streaming should not be deferred indefinitely.

### On Quality Specifically

We **cannot measure answer quality from telemetry alone**. `finish_reason` tells us about completion, not correctness. I would work with your team to build an evaluation suite using OpenAI's Evals API:
- 200+ representative questions with known-good answers
- Scored by domain experts or LLM-as-judge (GPT-4o grading GPT-4o-mini, for example)
- Metrics: accuracy, completeness, faithfulness to source documents, format compliance
- Every change (model, prompt, routing) gets tested against this benchmark before deployment
- This makes quality **visible and measurable** rather than anecdotal

---

## 5. How I Would Ensure Deployment

### 5.1 Stakeholder Communication

**Weekly executive summary** (CIO/CTO): Three metrics — system availability, cost per request trend, median response time — with a one-sentence narrative each. No jargon. Example: *"Availability improved from 91.8% to 97.5% during peak hours after the inventory circuit breaker was deployed. Cost per request stabilized for the first time in 3 weeks. On track for pilot team onboarding in week 7."*

**Bi-weekly technical review** (engineering leads): Working session with dashboards to review in-flight changes, eval results, and next sprint priorities. I would share the OpenAI usage dashboard data alongside your internal metrics.

**Ad-hoc incident communication:** Within 2 hours of any degradation, structured as: what happened, what's impacted, what we're doing, when we'll update next.

### 5.2 Roles and Ownership

As your OpenAI ASE, I own **advisory, escalation, and optimization guidance**. Concretely:

**What I do:**
- Help diagnose the prompt growth (review prompt structure, identify accumulation mechanism)
- Advise on prompt structure for maximum cache hit rates
- Facilitate the model evaluation (help design evals, run side-by-side comparisons)
- Connect you with OpenAI's fine-tuning resources, Cookbook RAG best practices, and evaluation tooling
- Escalate rate-limit or API reliability issues internally at OpenAI
- Review your architecture for scaling readiness
- Facilitate usage tier upgrades if needed

**What your engineering team owns:**
- Fixing the inventory service
- Adding Redis/ElastiCache caching
- Restructuring the Lambda pipeline (parallelization, streaming, circuit breakers)
- Implementing graceful degradation and partial-answer logic
- Deploying monitoring, alerting, and feedback mechanism
- Implementing prompt structure changes and `max_tokens` fix
- Running load tests
- Building the dashboard and evaluation pipeline

I'll provide a **prioritized action list** with specific recommendations for each sprint and join architecture reviews as needed. I'm not writing your code — I'm making sure every OpenAI-related decision is informed, validated, and set up for success.

### 5.3 Implementation Waves

| Wave | Timeline | Actions | Key Outcome |
|------|----------|---------|-------------|
| **Wave 1: Quick Fixes** | Week 1 | Raise max_tokens (A5), remove tool defs from policy_only (A5), review OpenAI tier (A5), set up alerting (A6), begin prompt investigation (A1) | Truncation eliminated. Alerting live. Cost visibility. |
| **Wave 2: Stabilize** | Weeks 2–4 | Fix prompt growth (A1), remediate inventory (A2), add feedback (A3), enable logging (A4), implement circuit breakers + graceful degradation (A2), proper retries (A14), parallelize calls (A7) | Peak error rate <1%. Cost stabilized. Feedback flowing. Quality data collection begins. |
| **Wave 3: Optimize** | Weeks 4–8 | Chunk budget (A8), prompt caching (A9), model routing eval (A10), function calling optimization (A12), shared caching infrastructure (A13), streaming (A16) | TTFT ~150ms. Cache hit rates 20–50%. Prompt caching active. |
| **Wave 4: Expand** | Weeks 8–12 | Eval pipeline (A15), Lambda concurrency increase (A17), per-team cost attribution (A19), load test at 4× (A20), pilot one team, expand to remaining two | Eval pipeline live. 3 new teams onboarded. All SLAs met. |

### 5.4 Success Criteria and Expansion Gates

| Metric | Current | Target (Pre-Expansion) | Target (Post-Expansion) |
|--------|---------|----------------------|------------------------|
| Overall availability | 97.2% | > 99% | > 99% |
| Peak-hour availability | 91.8% | > 97% | > 97% |
| p50 response time | 1,012ms | < 1,000ms | < 1,000ms |
| Perceived TTFT | ~1,000ms | ~1,000ms (pre-streaming) | **~150ms (with streaming)** |
| Cost per request trend | +6.2%/day | Flat | Flat |
| Response truncation rate | 5.2% | < 1% | < 1% |
| Prompt tokens (daily avg) | Growing +86/day | Stable | Stable |
| Cache hit rate (policy) | 5.8% | > 30% | > 30% |
| OpenAI prompt cache rate | 0% | > 50% | > 50% |
| 7-day repeat usage | ~0% | Baselined | > 20% target |
| Quality (feedback) | Not measured | Feedback flowing | > 80% thumbs-up |

**Expansion gates:** All pre-expansion targets met for two consecutive weeks before onboarding the pilot team. If any metric regresses after pilot launch, we pause before adding the remaining teams.

I would track these weekly using a combination of your CloudWatch logs and the OpenAI usage dashboard, and present them in the weekly executive summary.

---

## Appendix A: Summary of OpenAI Platform Features Referenced

| Feature | Relevance to This System | Status |
|---------|-------------------------|--------|
| **Prompt Caching** | 50% input discount, up to 80% latency reduction. Automatic for prefixes ≥1,024 tokens. Requires static prefix first. | Not active (0% hits). Quick win with prompt restructuring. |
| **GPT-4o-mini** | 94% cheaper than GPT-4o. Strong on straightforward Q&A. | Not used. Evaluate for policy_only route. |
| **Structured Outputs** | 100% schema adherence for function call responses. `strict: true` parameter. | Not used. Apply to inventory tool calls. |
| **Fine-tuning (GPT-4o)** | Encode behavioral instructions into weights. Eliminate prompt bloat. $25/M training. | Not used. Medium-term optimization. |
| **Evals API / Framework** | Structured tests for model quality. Dashboard + API + open-source. | Not used. Critical before any model/prompt change. |
| **Usage Tiers** | Automatic rate limit increases with spend. Tier 1–5 system. | Unknown tier. Verify before expansion. |
| **Batch API** | 50% discount, 24h turnaround. | Not viable for real-time assistant. Useful for offline eval runs. |
| **Function Calling best practices** | Describe when (and when not) to use each function in the system prompt. | 74 unnecessary tool calls on policy_only suggest this isn't configured. |
| **`prompt_cache_key`** | Optional parameter to improve cache routing for high-traffic shared prefixes. | Not used. Apply after basic caching works. |
| **Extended cache retention** | 24-hour cache retention instead of 5–10 minutes. | Not used. Evaluate after basic caching. |
| **OpenAI Cookbook** | RAG best practices, prompt engineering patterns, fine-tuning guides. | Reference for implementation guidance. |

---

## Appendix B: Request Flow Diagrams by Route

### policy_only Route (49.1% — 589 requests)

```
Employee ──▶ Lambda ──▶ [Step 1] Retrieval (71ms avg) ──▶ [Step 2] OpenAI GPT-4o (753ms avg) ──▶ Response
                               │                                                                   (909ms avg)
                         Cache: 5.8% hit
                         Timeout: 250ms
                         Errors: 0%

  Timeline:
  Retrieval:  ██               71ms
  OpenAI:       ████████████████████████████████████  753ms
  Overhead:                                           ██  76ms
  ──────────────────────────────────────────────────────────
  Total:                                               909ms
```

### inventory / mixed Route (51% — 611 requests)

```
Employee ──▶ Lambda ──▶ [Step 1] Retrieval (70ms) ──SERIAL──▶ [Step 2] Inventory (337ms) ──▶ [Step 3] OpenAI (730ms)
                               │                          │                                         │
                         Cache: 5.8%                Cache: 5.6%                                Response
                         Timeout: 250ms             Timeout: 900→1300ms                        (1,253ms avg)
                         Errors: 0%                 Errors: 2.3%
                                                    Retries: 7.4%

  Timeline (CURRENT — serial):
  Retrieval:  ██                           70ms
  Inventory:     ████████████              337ms   ◀── WASTED: waits for retrieval to finish
  OpenAI:                     ████████████████████████████████  730ms
  Overhead:                                                     ██  70ms
  ─────────────────────────────────────────────────────────────────────
  Total:                                                        1,253ms

  Timeline (PROPOSED — parallel):
  Retrieval:  ██                           70ms  ┐
  Inventory:  ████████████                 337ms ┘ Promise.all() — overlapped
  OpenAI:                 ████████████████████████████████  730ms
  Overhead:                                                 ██  70ms
  ─────────────────────────────────────────────────────────────────────
  Total:                                                    ~1,183ms  (saves 70ms)
```

---

## Appendix C: Root Cause Pattern

This system was built as an MVP and deployed to production without the additional engineering that production demands. This is common and not a criticism — it's the expected trajectory for any fast-moving team.

| Problem | MVP Shortcut | Production Requirement |
|---------|-------------|----------------------|
| Serial downstream calls | Sequential `await` | Parallel calls with partial-failure handling |
| All errors in peak hours | Default Lambda auto-scaling | Provisioned concurrency for predictable peaks |
| No graceful degradation | All-or-nothing logic | Partial responses with clear communication |
| Ineffective caching (5–6%) | Exact-match query cache | Component-level caching with structured keys |
| Rate-limit errors (429) | Default OpenAI tier | Tier sized for peak volume |
| Naive retries, no classification | Retry once immediately | Exponential backoff + circuit breakers + error classification |
| Truncated responses (5.2%) | Conservative `max_tokens` | Adequate headroom (1,024) |
| No prompt caching | Unstructured prompt template | Static prefix first for cache activation |
| GPT-4o for everything | Single model | Complexity-based model routing with eval validation |
| No streaming | Synchronous response | Streaming for real-time UX |
| No quality measurement | No feedback, no evals, no logging | Feedback + golden Q&A + eval pipeline + dashboard |
| No adoption tracking | Deployed and assumed working | Repeat usage metrics, feedback, onboarding |

**The good news:** None of these are fundamental. The architecture is sound at its core — Lambda orchestrating a vector store, an inventory service, and OpenAI. Every issue is fixable with targeted engineering, not a rewrite. The most impactful changes (fix prompt growth, raise max_tokens, parallelize calls, add feedback) are also among the lowest-effort.
