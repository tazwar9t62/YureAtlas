# Durable local scheduling

Automatic scheduling applies to real experiments. Synthetic demo work remains explicit. Schedule decisions are stored separately from worker heartbeat updates: a live worker does not prove that its scheduled decisions have been committed.

The worker reconciles on startup and from a dedicated timer every 30 seconds. The timer waits for elapsed time and then reads the current aware UTC clock; it does not replay an in-memory list of cron callbacks. This keeps scheduling active during a long worker job and lets the next tick reconsider current due work after sleep or a wall-clock change. Initial or later failures are logged and retried at the next tick without advancing the decision state. Shutdown stops and joins the timer before releasing the worker's singleton connection.

The former APScheduler configuration used a 60-second misfire grace period. Its [misfire/coalescing documentation](https://apscheduler.readthedocs.io/en/3.x/userguide.html#missed-job-executions-and-coalescing) explains why an overdue callback can be discarded. Inspection of the installed 3.11.3 scheduler also showed that it enumerates missed fire times before coalescing them. The durable planner therefore computes the latest due occurrence directly and keeps recovery independent of callback delivery.

## Due work and catch-up

| Work | JST schedule | Recovery policy |
| --- | --- | --- |
| Recent USGS ingestion | Every 15 minutes | One latest due slot per experiment; retrieve the two days ending at that slot. |
| Daily forecast | 23:40, targeting the next midnight | Queue only while the target is still future. An absent expired target becomes a missed decision. |
| Evaluation | 06:00 | One latest due daily occurrence; evaluate currently completed forecasts against available evidence. |
| Reconciliation | 06:30 | One latest due daily occurrence. For USGS, retrieve the pinned preceding 31 days and then evaluate in the same durable job. JMA uses its imported evidence without automatic network retrieval. |
| Candidate training | Sunday 03:00 | Recover only the latest due weekly occurrence. Pin its requested training endpoint to that occurrence; the model protocol resolves the last completed JST day. Promotion remains explicit. |

Safe work is coalesced to its latest due occurrence, rather than replaying every missed ingestion, evaluation or training callback. Occurrences before an experiment existed are excluded. The canonical due time determines both the logical key and any retrieval/training interval. For example, restarting before 06:30 cannot consume that day's future reconciliation key. A failed USGS reconciliation refresh prevents its evaluation from starting; partial retrieval can still produce explicitly unavailable observations under the normal evaluation rules.

Daily forecast identity remains the experiment plus its canonical UTC target start. Startup at 23:39 creates no upcoming daily job; 23:40 through the instant before midnight can queue it once. An existing job wins any unique-key conflict unchanged, including an existing failed, cancelled or missed decision. Recovery never fabricates an on-time publication or silently retries a deliberately cancelled decision.

## Decision transaction and bounds

Job insertion and scheduler-state advancement share one database transaction, serialized by a dedicated advisory lock. A failure rolls back both, so the next tick can repeat the decision without a false completed watermark. Worker heartbeat updates cannot advance this state.

Per reconciliation, expired forecast history is limited to 31 target dates, including today's midnight and the preceding 30 midnights. The upcoming midnight is considered separately. A retained truncation record identifies an older interval omitted from individual recovery records; it is not evidence that every date in that interval was a newly missed forecast. Existing records remain intact. Normal ticks preserve the last truncation information.

The planner supports at most 100 real experiments in one automatic decision transaction. It reads one extra row to detect excess and fails with an actionable error before inserting jobs or advancing state. Inserts use bounded batches. This prevents an unnoticed partial scan from claiming that all experiments were reconciled.

The maximum reconciled timestamp never moves backwards. The separate observed timestamp records the current clock and bounds elapsed recovery after a backward jump; current and upcoming targets are still reconsidered using their unique keys. Previously recorded decisions are preserved. This is recovery from missed execution, not a guarantee that a misconfigured system clock represents real UTC correctly.

`SCHEDULE_ENABLED=false` is a hard process-level disable: no automatic trigger or catch-up work is created. On a later enabled start, the same bounded catch-up policy applies. Persisted settings and their effective/applied state are covered by T05.4; ownership, cancellation and stable execution recovery are covered by T05.3. See the [active tracker](remaining-tasks.md) for their current status.
