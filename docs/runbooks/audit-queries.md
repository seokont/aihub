# Auditing an approval

How to read the trail for one approval, and how to tell the three outcomes apart. Written because a live
run was approved and executed nothing, and the audit could not say which of three things had happened.

## The lifecycle, in order

```
approval_requested                  a write needed a human
agent.run awaiting_approval         the graph paused
approval.decided                    a human answered; the decision is final from here
agent.run_resumed                   whether the run continued, and why not if it did not
tool_executed_after_approval        one row per step this decision executed
```

`agent.run_resumed` is written by the gateway for **every** decision, including the ones that could not
continue, and its `result` carries the reason:

| `result` contains | meaning |
| --- | --- |
| `resumed=True reason=ok` | the run continued and at least one executed step named this approval |
| `resumed=True reason=continued_without_executing` | the run continued but the approved call did not run ? look for the agent's `observe_noop` log, which names the reason (`pending_call_displaced`, `nothing_to_execute`, `no_steps`) |
| `resumed=False reason=no_thread_id` | the approval row has no `thread_id`, so the runner was never called |
| `resumed=False reason=resume_failed` | the resume raised; the detail carries the exception type and message |

## Success versus failure of an approved write

**One action, two outcomes** ? deliberately, so "did an approved write run?" stays a single query:

```sql
-- every approved write that actually executed
select ts, tool, result from audit_log
 where action = 'tool_executed_after_approval' and result = 'ok';

-- every approved write that executed and failed, with the tool's error code
select ts, tool, result from audit_log
 where action = 'tool_executed_after_approval' and result like 'error:%';

-- approved writes that never reached a conclusion: no row at all
select a.id, a.tool, a.status from approvals a
 where a.status = 'approved'
   and not exists (select 1 from audit_log l
                    where l.approval_id = a.id
                      and l.action = 'tool_executed_after_approval');
```

The third query is the one that found the live bug. `result` is the outcome; the **absence** of the row
means the call never completed, which is a different problem from any `error:` row.

## Reading one run's state

`scripts/inspect_run.py` is read-only and answers "why did the approved call not execute":

```bash
DATABASE_URL=postgresql+asyncpg://<user>:<pw>@127.0.0.1:55432/<db>   uv run --group dev python scripts/inspect_run.py --approval-id 9f6f2efd
```

`--approval-id` takes a prefix and reads `thread_id`/`trace_id` from the approval row, so a mismatch
between the two is not misread as a missing checkpoint. `--list-threads 20` shows which threads actually
have checkpoints, and `--thread-id run-...` inspects one directly.
