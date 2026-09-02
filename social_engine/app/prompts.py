"""TBTX copy chain. Flagship check plus per-surface rules."""

from __future__ import annotations

import json

from app.models import CTA_LINE, CopyBundle, GrokChatMessage, GrokCopyRequest

SYSTEM_PROMPT = """You write TransformBy10X (TBTX) social copy.

You are not a hype machine. You write like a person talking to a person who is tired of being the unpaid intern of their own AI stack.

LOCKED PUBLIC CTA (every surface except Reddit, which gets the URL once at the end, quietly):
Start Here → https://transformby10x.ai/

Do not invent a different CTA. Do not say Take the Scan. Do not say Find the Blockage. Do not say Take the Digital Fog Diagnostic. The locked line is Start Here.

VOICE
- Contractions. If it sounds written, it failed.
- No em dashes. No en dashes. Use a period, a comma, or a new sentence.
- Keep the CTA arrow as written above. That arrow is not a dash.
- No hype. No manufactured urgency. No fake metrics, fake proof, fake prices, fake offers.
- Do not mention BizBuilders, BizBot, FLOW, CARE, GOAL, FAAS, Hermes, Agent Zero, or a product menu.
- Do not invent prices. Do not invent a program name that is not in the brief.
- Natural voice. Short sentences mixed with a few longer ones.

MUST ARC (every piece of copy walks this, even if some stages are one sentence):
1. Mirror: name a lived moment so the reader feels seen.
2. Understand: name the pattern (Digital Fog: tools multiplied faster than context).
3. Solve: one honest next step, not a stack of tools.
4. Transform: a believable lighter Tuesday, not a fantasy of full autonomy.

FLAGSHIP CHECK
If the brief is for the flagship campaign or is empty of a competing story, treat it as flagship.
Flagship lockups (two units, never a four-line stack):
- Headline unit: AI Created a Job. / (Nobody wanted.) tight under the hook.
- Mantle unit, separate: Managing Digital Fog / Start Here
Do not put Managing Digital Fog between the hook and (Nobody wanted.).
If the brief is clearly a different story, still keep voice, CTA, and MUST. Do not force the lockups onto a mismatched brief. Set flagship=true only when the lockups belong.

PRIORITIES
Video content, visual style, brand. Motion first. Stills are poster frames from motion, not clipart, not SaaS gradients, not fake dashboards.

SURFACE RULES
- linkedin: 150-250 words. A real post a founder would actually publish. No emoji walls. MUST arc in full.
- x: 5-7 tweet thread as an array of strings. First tweet is the mirror. Last tweet is the CTA line. None of the tweets use hype.
- reddit: almost no promo. Talk like a commenter in a serious thread. One quiet URL at the end is enough. No lockup recitation. No "Start Here" chant.
- instagram: under 150 words, written to sit under a quote card. quote_line is the card text (one or two short lines).
- facebook: under 150 words, same quote-card job, slightly warmer, still no hype.
- youtube_tiktok_script: 60-90 second talking-head. First 15 seconds mirror a moment. No "today I am going to show you". Lived moment, why it happens, what changes first, one next action, CTA.

Return ONLY JSON with keys:
{
  "linkedin": string,
  "reddit": string,
  "instagram": string,
  "facebook": string,
  "x": [string, string, ...],
  "youtube_tiktok_script": string,
  "quote_line": string,
  "video_first": true,
  "visual_prompt": string,
  "flagship": boolean
}

visual_prompt describes one cinematic still that could be a poster frame from a motion piece. TBTX campaign look: editorial, photorealistic, human tension, not clipart. No fake UI. No logo soup.
"""


def build_messages(request: GrokCopyRequest) -> list[GrokChatMessage]:
    messages = [GrokChatMessage(role="system", content=SYSTEM_PROMPT)]
    if request.mode == "revise":
        rejected = request.rejected_copy.model_dump() if request.rejected_copy else {}
        user = (
            "Revise this draft. Use ONLY the original brief, the rejected copy, "
            "and the latest feedback. Do not add new offers, prices, metrics, or brands.\n\n"
            f"ORIGINAL BRIEF:\n{request.brief}\n\n"
            f"REJECTED current_copy_json:\n{json.dumps(rejected, indent=2)}\n\n"
            f"LATEST feedback_text:\n{request.feedback_text or ''}"
        )
        messages.append(GrokChatMessage(role="user", content=user))
        return messages

    user = (
        "Write a fresh TBTX package for this brief. Run the flagship check. "
        "Honor every surface rule. CTA is locked.\n\n"
        f"BRIEF:\n{request.brief}"
    )
    messages.append(GrokChatMessage(role="user", content=user))
    return messages


def fake_copy(request: GrokCopyRequest) -> CopyBundle:
    """Deterministic on-brand package used when SOCIAL_ENGINE_FAKE=1."""
    brief = request.brief.strip() or "the extra job the tools assigned to you"
    revised = request.mode == "revise"
    prefix = "After your note: " if revised else ""
    feedback = (request.feedback_text or "").strip()
    extra = f" You asked to tighten this: {feedback}." if revised and feedback else ""

    quote = "AI created a job. Nobody wanted it."
    linkedin = (
        f"{prefix}You added agents so you'd have less to do. Now they ping you when the tools "
        f"don't agree. That's the brief in the room: {brief}.{extra}\n\n"
        "The extra job isn't 'using AI.' It's moving information between tools and translating "
        "context between people. Nobody applied for that role. We call the condition Digital Fog. "
        "Tools multiplied faster than context, and you became the chaperone.\n\n"
        "You don't fix that with another model. You name the job, then you take one honest next "
        "step instead of stacking more software on a pile that already doesn't hold.\n\n"
        "The Tuesday you want back starts when the work is current and the handoff doesn't need "
        "a babysitter. That's the transform: not full autonomy theater, just a day that starts "
        "with the work.\n\n"
        f"{CTA_LINE}"
    )
    reddit = (
        f"{prefix}I thought more agents would remove work. Then I became the person reconciling "
        f"what they disagreed about. The brief I keep coming back to: {brief}.{extra}\n\n"
        "The invisible job is moving information between systems and translating context between "
        "teams. If that's you, you're not behind. You're doing a job the stack invented.\n\n"
        "https://transformby10x.ai/"
    )
    instagram = (
        f"{prefix}You weren't supposed to become the full-time translator between tools, "
        f"decisions, and people. {brief}.{extra}\n\n"
        "Managing Digital Fog starts by seeing the extra work clearly.\n\n"
        f"{CTA_LINE}"
    )
    facebook = (
        f"{prefix}The tools got faster. The work got foggier. You're still chasing the handoff. "
        f"{brief}.{extra}\n\n"
        "Name the job. Then find your fog.\n\n"
        f"{CTA_LINE}"
    )
    x = [
        "You added agents so you'd have less to do.",
        "Now they ping you when the tools don't agree.",
        "The extra job landed on you. Digital Fog.",
        "It isn't 'using AI.' It's chaperoning the handoff.",
        "Name the job before you buy another tool.",
        "Find your fog.",
        CTA_LINE,
    ]
    if revised:
        x[0] = "You asked for a tighter open, so here it is."
        x[1] = "You added agents so you'd have less to do. They still ping you."
    script = (
        f"{prefix}[0-12s] You added agents so you'd have less to do. Now they ping you when "
        f"the tools don't agree.\n"
        "[12-30s] That extra job has a name. Digital Fog. Tools multiplied faster than context.\n"
        "[30-55s] You don't need another model. You need to see the work the stack assigned to you.\n"
        f"[55-80s] One next step. Not a catalog. {CTA_LINE}\n"
        "[80-90s] Sit with that. Then go."
    )
    visual = (
        "Cinematic still, poster frame from motion: a person at a dark desk, several screens "
        "out of agreement, warm practical lamp, cool spill from the monitors, editorial "
        "photoreal, negative space on the right for type. Not clipart. Not a fake dashboard. "
        "TBTX campaign look."
    )
    return CopyBundle(
        linkedin=linkedin,
        reddit=reddit,
        instagram=instagram,
        facebook=facebook,
        x=x,
        youtube_tiktok_script=script,
        quote_line=quote,
        video_first=True,
        visual_prompt=visual,
        flagship=True,
    )
