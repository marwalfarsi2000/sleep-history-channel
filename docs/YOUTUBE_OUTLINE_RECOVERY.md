# Publishing and outline recovery — 2026-11-15

Base: `51b363fe4c675465415863a412348ce4f11e85d5`. No live publishing,
episode regeneration, credential changes, or workflow dispatch was performed.

## Verified evidence

- Run [39868788973](https://github.com/marwalfarsi2000/sleep-history-channel/actions/runs/39868788973)
  failed with `unauthorized_client` on 2026-10-22.
- Run [40042413786](https://github.com/marwalfarsi2000/sleep-history-channel/actions/runs/40042413786)
  was green but returned `PUBLISH_BLOCKED_AUTH / REAUTHORIZE_YOUTUBE_OAUTH` on
  2026-10-23. A green workflow is **not** evidence of successful publication.
- The local external credential file supplies Client ID and Client Secret, but
  no Refresh Token. GitHub's encrypted secret values are not readable through
  the available API permission. Current live credential binding is NOT_VERIFIED.
- Read-only Internet Archive metadata checks found `14_video_final.mp4` and
  `APPROVED_FOR_PUBLISH` for these existing identifiers:
  - `lan-tukmil-alqissa-20261022-ghadames-oasis-pottery-cr`
  - `lan-tukmil-alqissa-20261023-muscat-old-market-herbs-r`
  - `lan-tukmil-alqissa-20261024-nile-delta-papyrus-making`
- Muscat has the legacy `youtube_upload_status=rejected` without a video ID.
  Check the owning channel before any replacement: old code classified every
  resumable-upload 4xx as rejected, which does not prove nothing was uploaded.

## Code changes

- Require fresh authoritative publishing state and a durable upload-intent
  acknowledgement. Archive-only acknowledgements require metadata readback.
- Propagate discovery verification errors. An empty/failed search cannot clear
  an uncertain upload. Preserve known video IDs and human approval across OAuth
  errors. A missing known video or absent read scope requires owner review.
- Preserve the existing workflow concurrency group and review decision path.
- Repair BOM/fences/trailing commas without changing quoted text or inventing
  missing values. Reject ambiguous documents, duplicate keys and nonfinite
  numbers. Preserve the existing trailing-commentary tolerance.
- Keep two model-call attempts, full schema/quality validation, an atomic raw
  response checkpoint bound to the prompt hash, and validated-response reuse.
  Completed episodes bypass generation. No model was called during testing.
- Use binary UTF-8 Git plumbing: Windows text stdin previously created a file
  literally named `state.json\r`. This failure was reproduced on the base.

## Human OAuth gate

Do not send a token or secret in chat or commit it. Reauthorize the **same**
OAuth client and intended YouTube channel, using offline access and explicit
consent. For the current `approved_direct_public` mode use
`youtube.upload` plus `youtube.readonly` for safe readback/recovery.
`private_verify_public` additionally needs a management scope; do not silently
change the selected publish mode to evade capability checks.

For an existing **web** OAuth client, Google's
[OAuth Playground](https://developers.google.com/oauthplayground/) supports
“Use your own OAuth credentials”; its redirect must be registered as
`https://developers.google.com/oauthplayground` in that client's Google Console.
Use Offline / Consent Screen, authorize the required scopes on the correct
channel, then exchange the authorization code. Use neither Playground's default
client nor a refresh token issued to a different client. For a Desktop client,
use its supported loopback flow instead; do not assume the web redirect works.
Account login, client type/redirect and consent require the owner.

Save the matching trio securely in the existing
[repository Actions secrets](https://github.com/marwalfarsi2000/sleep-history-channel/settings/secrets/actions),
and in the authorized external secret store if a local preflight is wanted.
No secret value belongs in an issue, PR, screenshot or command argument.

Presence-only (no network):

```sh
python scripts/youtube_oauth_check.py --offline
```

After owner consent and secure binding, run the same script **without**
`--offline` in the credential-bearing environment. This refreshes OAuth and
checks scopes only; it never builds an upload client or publishes a video.
A `PASS` is required before resuming publication; it is not a publication test.

After code review/activation and successful OAuth preflight, use the existing
**Sleep History Review Decision** workflow: action `publish`, identifier of the
already-approved archive item. Do not run `produce` to recover OAuth. Reconcile
any uncertain prior upload first; never clear its marker merely to retry.

Official references: [OAuth offline access](https://developers.google.com/identity/protocols/oauth2/web-server#offline),
[upload scopes](https://developers.google.com/youtube/v3/docs/videos/insert).

## Tests

`evidence/recovery-20261115/receipt.json` and the two final JUnit files record
the disjoint focused suites. Python socket connections were forbidden during
both suites. Tests use synthetic credentials, videos, isolated temporary Git
remotes and checkpoints; no production state or paid API was used.
