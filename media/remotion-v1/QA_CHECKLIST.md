# QA Checklist — independent visual review

Current state: NOT_RUN. No test frames or video were rendered, no visual QA PASS granted.

- [ ] Confirm researched episode title and source-grounded visual brief; generic silhouettes are not claimed as real archaeological sites.
- [ ] Check storyboard timing and all props; no secrets, personal data or unsupported historical claims.
- [ ] Verify 16:9, 1920×1080, 24fps, 180px safe margins and offline Arabic fonts.
- [ ] Test frames 0, 72, 600, 720, 1320, 1440, 2100 using external worker ONLY; inspect at 960×540 and mobile scale.
- [ ] Arabic RTL shaping, punctuation, diacritics and word order render properly; text does not clip or overlap.
- [ ] Confirm no bright flicker, hard cut, attention-grabbing movement, neon or flashing.
- [ ] Around 30s and 60s transitions inspect 5s crossfade; no unwanted black gap.
- [ ] Intro title fades fully out; no music, SFX, unsolicited narration or recurring captions.
- [ ] Color and historical tone matches dark navy/ivory/amber; mark any perceived inaccuracy FAIL.
- [ ] Any FAIL triggers targeted correction, regeneration of affected test frames, and re-review.
- [ ] Record reviewer, visual QA PASS, evidence PNG paths, timestamp and template SHA before render.
- [ ] Only manually authorized external CPU worker render ≤300s, concurrency 1; verify codec/duration.
- [ ] Hash inputs/video SHA-256 and record task/run IDs, worker, reviewer, artifact provenance.
- [ ] Store only in approved private project-isolated B2/client storage after QA; no publication.
- [ ] Existing YouTube/Internet Archive owner approval remains unchanged.

GATE: BLOCKED_UNTIL_VISUAL_QA_PASS.
