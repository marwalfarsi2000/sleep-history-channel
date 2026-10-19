// Tiny Puter.js adapter: reference image + prompt in, one finished image file out. Python owns everything else.
// usage: node scripts/generate_image_puter.mjs <reference.png> <prompt.txt> <out.png> [quality]
// The auth token is read from PUTER_AUTH_TOKEN only; nothing secret is ever printed. stdout is one JSON line.
import { createRequire } from 'node:module';
import { readFileSync, writeFileSync } from 'node:fs';

const MODEL = 'openai/gpt-image-2.5-sunburst';
const WIDTH = 1920;
const HEIGHT = 1080;

const fail = (code, detail = '') => {
    console.log(JSON.stringify({ ok: false, code, detail: String(detail).slice(0, 200) }));
    process.exit(2);
};

const token = process.env.PUTER_AUTH_TOKEN || '';
if (!token) fail('PUTER_AUTH_MISSING');
const redact = (text) => String(text ?? '').split(token).join('***');
const errorInfo = (error) => {
    const nested = error && typeof error.error === 'object' ? error.error : {};
    return {
        status: error?.status ?? nested.status,
        code: error?.code ?? nested.code,
        message: redact(error?.message ?? nested.message ?? error),
    };
};
// Read-only check (no generation, nothing charged): `--check` reports whether the token works and the free allowance left.
if (process.argv[2] === '--check') {
    try {
        const require = createRequire(import.meta.url);
        const { init } = require('@heyputer/puter.js/src/init.cjs');
        const puter = init(token);
        const user = await puter.auth.getUser();
        const info = (await puter.auth.getMonthlyUsage())?.allowanceInfo ?? {};
        console.log(JSON.stringify({ ok: true, auth: 'PASS', user_present: Boolean(user), allowance_total: info.monthUsageAllowance ?? null, allowance_remaining: info.remaining ?? null, unit: info.unit ?? 'raw' }));
        process.exit(0);
    } catch (error) {
        const { status, code, message } = errorInfo(error);
        fail('PUTER_AUTH_FAILED', `${status ?? ''} ${code ?? ''} ${message}`);
    }
}
// Free-only gate: this adapter refuses to spend unless the owner has verified the account can only draw on its free allowance.
if (process.env.PUTER_FREE_ONLY_VERIFIED !== '1') fail('PUTER_FREE_ONLY_NOT_GUARANTEED');

const [referencePath, promptPath, outPath, quality = 'medium'] = process.argv.slice(2);
if (!referencePath || !promptPath || !outPath) fail('PUTER_BAD_ARGS');

try {
    const require = createRequire(import.meta.url);
    const { init } = require('@heyputer/puter.js/src/init.cjs');
    const puter = init(token);

    // Never start a generation that the free monthly allowance cannot clearly cover (purchased credits are never used).
    const readRemaining = async () => (await puter.auth.getMonthlyUsage())?.allowanceInfo?.remaining;
    const before = await readRemaining();
    const minimum = Number(process.env.PUTER_SAFETY_BUFFER || 50); // a safety buffer, not the expected cost of an image
    if (!(typeof before === 'number' && before >= minimum)) fail('PUTER_FREE_ALLOWANCE_EXHAUSTED', `allowance ${before} below safety buffer ${minimum}`);

    const reference = readFileSync(referencePath).toString('base64');
    const result = await puter.ai.txt2img({
        prompt: readFileSync(promptPath, 'utf8'),
        model: MODEL,
        quality,
        width: WIDTH,
        height: HEIGHT,
        ratio: { w: 16, h: 9 },
        input_image: reference,
        input_image_mime_type: 'image/png',
    });

    const src = typeof result === 'string' ? result : result?.src;
    let bytes;
    if (typeof src === 'string' && src.startsWith('data:')) {
        bytes = Buffer.from(src.slice(src.indexOf(',') + 1), 'base64');
    } else if (typeof src === 'string' && /^https:\/\//.test(src)) {
        const response = await fetch(src);
        if (!response.ok) fail('PUTER_BAD_RESPONSE', `download HTTP ${response.status}`);
        bytes = Buffer.from(await response.arrayBuffer());
    } else {
        fail('PUTER_BAD_RESPONSE', 'unrecognised image result');
    }
    writeFileSync(outPath, bytes);
    let after = null;
    try { after = await readRemaining(); } catch { /* the image is already saved */ }
    console.log(JSON.stringify({ ok: true, bytes: bytes.length, allowance_before: before, allowance_after: after }));
    process.exit(0); // puter.js leaves sockets open
} catch (error) {
    const { status, code, message } = errorInfo(error);
    const funds = status === 402 || code === 'insufficient_funds' || code === 'subscription_required' || error?.metadata?.usage_limited === true;
    const auth = status === 401 || code === 'token_auth_failed' || code === 'forbidden';
    fail(funds ? 'PUTER_FREE_ALLOWANCE_EXHAUSTED' : auth ? 'PUTER_AUTH_FAILED' : 'PUTER_REQUEST_FAILED', `${status ?? ''} ${code ?? ''} ${message}`);
}
