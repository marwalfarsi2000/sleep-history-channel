# MEDIA PIPELINE — HERMES Remotion V1

Basis: HERMES MASTER EXECUTION BLUEPRINT v5 FINAL (22 Oct 2026) §19.1. Draft implementation: media/remotion-v1. Current channel: «لن تُكمل هذه القصة». Reviewed README, settings, master production rules, owner behavior invariants and automation review. Existing channel uses low-stakes, historically researched adult Arabic narration and Python/FFmpeg. Publishing requires explicit APPROVE_AND_PUBLISH. Existing 3% pink noise and voice QC stay untouched.

## Pipeline and gates

1. BRIEF: fact-checked natural-language episode brief (see BRIEF.md), no unsupported historic visuals.
2. STORYBOARD: the short sample has 3 sequential scene beats; source: STORYBOARD.md and data/sample.json.
3. STYLE: data/style.json locks midnight palette, 24fps, 1920×1080, Arabic RTL, offline font fallbacks, 5-second crossfade, <=12px drift.
4. SKILL REVIEW: Remotion Agent Skills are UNTRUSTED suggestions until inspected; never install or run unknown code on Controller.
5. REACT/TYPESCRIPT: a single silent CalmHistorySleeper composition in src/index.tsx. Props are bounded to 15–300 seconds.
6. CONTRACT: dependency-free python3 scripts/check_contract.py plus TypeScript typecheck on external worker.
7. TEST FRAMES: seven low-scale PNG stills through scripts/test-frames.sh; do not render full video yet.
8. VISUAL QA: manual independent checklist in QA_CHECKLIST.md; failed observations require iterative targeted fixes, then new frames. No QA=PASS by CLI exit alone.
9. RENDER: CPU-only external worker, concurrency=1, short clip only after visual PASS and explicit authorization. Full 60–90-minute video should NOT be rendered with Remotion V1.
10. PROVENANCE: sha256 of input and short output plus task_id, run_id, commit_sha, template version, worker_id, reviewer, frame evidence, status, time.
11. PRIVATE STORAGE: Backblaze B2 or tenant/client storage via preconfigured external worker; private ACL, retention and credentials verified beforehand.
12. LATER FINAL ASSEMBLY: optionally loop/compose the short visual with existing FFmpeg worker and already QC-passed long narration, preserving explicit owner approval. Not implemented or run in this PR.

## Future worker commands — NOT EXECUTED

Only on a separate approved worker with adequate capacity, free-license eligibility, available Arabic fonts, no paid overage, no Controller installation, and no public deployment credentials:

    cd media/remotion-v1
    npm install --ignore-scripts
    npm run typecheck
    npm run test:contract
    npm run test:frames

Inspect all stills manually; declare VISUAL_QA=PASS with evidence only after independent signoff. Then, with owner authorization:

    npm run render:external-worker
    sha256sum data/sample.json out/loop.mp4

If Chromium/browser dependencies are required, prepare them on the worker, not on Hermes Controller. Stop if a paid plan, license or overage is required. There is no automated upload, render scheduler, CI job or deployment in this draft.

## Security and licensing

Remotion is free only for eligible individuals and small organizations (up to three people); verify eligibility for the exact use case before any commercial render. Never use paid Remotion Lambda/Cloud Run. No Snow Melt, Higgsfield, OpenAI, secrets, paid services, public publishing, n8n modifications, or changes to current Python/FFmpeg production. No root dependency changes. No automatic fallback. Any unverifiable QA, licensing or storage ACL ⇒ BLOCKED.

References: HERMES Blueprint §19.1; https://github.com/remotion-dev/remotion/blob/main/LICENSE.md ; https://www.remotion.dev/docs/cli/still ; https://www.remotion.dev/docs/cli/render .
