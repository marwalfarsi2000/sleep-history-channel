# Simplified Recovery Contract

The production pipeline uses one simple rule:

**Each stage may repair its own output once. Only transient external failures may retry the whole stage once.**

## Local repair

- Topic/research: validate once; no blind same-job loop.
- Outline: one initial generation plus one corrected response.
- Chapter length: one continuation appended to the saved chapter; never regenerate a saved chapter.
- Fact check: one correction pass followed by verification.
- Story quality: one final quality stage after fact check, one targeted repair, then one recheck.
- Late curiosity: rewrite only the late question sentences, not the whole ending.
- TTS/media stages keep their own bounded local validation.

## Global retry

Global `produce_with_recovery` retries only `TRANSIENT_RETRYABLE` external failures, once, after 30 seconds.

Validation failures, quota waits, cost blocks, owner actions, and code defects never start a blind same-job retry loop.

## Failure behavior

If the local repair is exhausted:

1. preserve the durable checkpoint;
2. stop the current run;
3. report the exact stage and error code;
4. let the next scheduled resume continue from saved work when appropriate.

No successful chapter or stage is regenerated merely because a later gate failed.
