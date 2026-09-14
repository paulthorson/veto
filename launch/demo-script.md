# Demo video script — 60 seconds

**Format:** screen recording + voiceover. No face cam needed. Record at 1080p,
terminal/IDE with a clean theme, phone visible for the WhatsApp beat.
Target: ~150 spoken words. Music: low, optional, cut under the refusal beat.

## The beats

### 0:00–0:05 — The hook
- **Visual:** black screen, terminal cursor. Type `search_jobs("backend engineer", remote_only=True)`.
- **On-screen text:** "My AI applies to jobs for me."
- **Voiceover:** "I built an AI that applies to jobs for me."
- **Beat:** pause. New line of on-screen text: "Sometimes, it says no."
- **Voiceover:** "And sometimes, it says no."

### 0:05–0:15 — The search
- **Visual:** results stream in — company, title, board on each row.
- **On-screen text:** "4 boards. Official APIs only."
- **Voiceover:** "It searches four job boards — public and official APIs
  only. No scraping."

### 0:15–0:28 — The WhatsApp grill
- **Visual:** phone screen: WhatsApp notification, then the chat. Five numbered
  questions appear: missing skill, unquantified experience, work authorization,
  salary band, why this company. Quick cuts of answers being typed.
- **On-screen text:** "It grills you before it applies."
- **Voiceover:** "Before it touches an application, it grills me — over
  WhatsApp — on the gaps between my resume and the job. No answers,
  no application."

### 0:28–0:38 — The tailoring
- **Visual:** side-by-side diff of resume before/after; a red strikethrough on
  an invented skill with a "blocked: honesty check" tooltip.
- **On-screen text:** "Tailored. Never invented."
- **Voiceover:** "It tailors my resume from real experience only. Try to make
  it invent a skill and the honesty check kills the application."

### 0:38–0:50 — THE REFUSAL (the moment people share)
- **Visual:** terminal: `apply_to_job(staff_ml_engineer, confirm=True)`.
  Output: `status: governance_blocked`, `qualification_score: 0.31`,
  `reason: severe experience gap`.
- **On-screen text (big, hold 3 seconds):** "REFUSED."
- **Voiceover:** "Here's the part no other apply-bot does. I told it to apply
  to a staff role I'm not qualified for. It refused. Score: 0.31. It won't
  lie for me — even when I ask it to."
- **Beat:** let the blocked output sit on screen in silence for 2 seconds.

### 0:50–0:57 — The queue
- **Visual:** `queue_list` showing 4 queued applications, "next in 4m 12s",
  a browser window with a filled form and the submit button unclicked,
  cursor hovering but not clicking.
- **On-screen text:** "Measured pace. You click submit."
- **Voiceover:** "Real applications queue at a measured pace — five a day
  max. It fills the forms. You click submit."

### 0:57–1:00 — CTA
- **Visual:** black screen.
- **On-screen text:** "The job bot that says no." / "[repo link]" / "Open source. MCP."
- **Voiceover:** "The job bot that says no. Link below."

## 30-second cutdown

| Time | Visual | Voiceover |
|---|---|---|
| 0:00–0:04 | Black screen, the two hook lines | "I built an AI that applies to jobs for me. And sometimes, it says no." |
| 0:04–0:12 | WhatsApp grill questions appearing | "It grills me on the gaps between my resume and the job — over WhatsApp." |
| 0:12–0:24 | The refusal output, "REFUSED." held | "I told it to apply to a role I'm not qualified for. It refused. It won't lie for me — even when I ask." |
| 0:24–0:30 | Black screen, tagline + link | "The job bot that says no. Link below." |

## On-screen text cheat sheet (for the editor)

1. "My AI applies to jobs for me."
2. "Sometimes, it says no."
3. "4 boards. Official APIs only."
4. "It grills you before it applies."
5. "Tailored. Never invented."
6. "REFUSED." (hold)
7. "Measured pace. You click submit."
8. "The job bot that says no." + link
