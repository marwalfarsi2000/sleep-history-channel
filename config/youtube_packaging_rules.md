# YOUTUBE EPISODE PACKAGING — REQUIRED

Return only these structured values: `episode_title_core`, `title_suffix`, `final_youtube_title`, `thumbnail_text`, `thumbnail_prompt`.

## Title

- Format: `[episode_title_core] | [title_suffix]`.
- Put the unique Arabic historical subject first. Prefer four to nine Arabic words.
- State the place, period, activity, route, civilization, or situation clearly and naturally.
- Avoid clickbait, false implications, repeated openings, repeated grammar, and words that hide the subject on mobile.
- Choose exactly one suffix from: قصة تاريخية للنوم 😴؛ قصة تاريخية هادئة للنوم 😴؛ ستنام قبل النهاية 😴؛ حكاية تاريخية للنوم 😴؛ رحلة هادئة إلى الماضي 😴؛ حكاية هادئة من الماضي 😴؛ قصة طويلة وهادئة للنوم 😴؛ رحلة تاريخية هادئة للنوم 😴.
- Match the suffix to the episode. Never repeat the immediately previous suffix and avoid the previous three when another fits. Balance usage over time. Use exactly one sleep emoji and no other emoji.

## Thumbnail text

- Two to four Arabic words, concrete, natural, instantly readable, and specific to this episode.
- No emoji, sentence, sleep suffix, or generic phrase such as قصة للنوم، قصة تاريخية، حكاية للنوم، للنوم، قبل النوم.
- Complement rather than duplicate the title. Avoid recent labels.

## Thumbnail prompt

Write one complete English prompt for a premium adult historical sleep-story thumbnail, customized to the exact topic and setting.

- Calm illustrated realism with subtle painterly texture and premium storybook quality; believable, not hyperrealistic, glossy, fantasy, tourism photography, or generic AI imagery.
- Quiet, cozy, sleepy, nostalgic, slightly mysterious and historically immersive.
- Low-key night lighting: dim lanterns, candles, warm windows, restrained moonlight behind clouds, optional light haze or context-appropriate rain.
- Palette: deep midnight navy, near-black blue, charcoal, muted warm gold, warm brown, muted amber, aged ivory and restrained dark olive.
- One simple memorable scene, one dominant focal subject, intimate rather than monumental. The actual place or an everyday setting from the episode must be unmistakable.
- Describe the background scene ONLY (16:9 horizontal ratio). Request a clean dark area occupying about twenty-five to thirty-five percent reserved for text overlay.
- Do NOT request the image model to render any Arabic text, letters, typography, logos, or watermarks in the image. The Arabic thumbnail label from `thumbnail_text` will be overlaid locally by Python after image generation.
- Mobile-first: one scene, one focal point, strong silhouette, no reliance on tiny details.
- People optional; at most two, natural pose, no camera eye contact, giant face, portrait, emotion, or hero pose.
- Strictly avoid rulers staring at camera, travel posters, perfect architecture, giant moon, magic, neon, HDR, excessive orange, oversaturated blue-orange, action, battle, weapons, crowds, collage, arrows, circles, clickbait, modern objects, artifacts, logos, and watermarks.
- Keep the prompt concise, mobile-first, and strictly under 1700 characters.

## Anti-repetition and validation

Read the previous ten published records. Avoid repeated title structures, suffixes, labels, scene types, focal subjects, and interchangeable generic city scenes. Ensure historical accuracy, exact Arabic text, one focal subject, brand palette, mobile readability, and no misleading element. Regenerate only fields reported as failing.
