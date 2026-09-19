# Launch thread — X (8–10 posts)

**1/**
Everyone's building AI that spams 1,000 job applications while you sleep.

I built the opposite: a job-search agent that refuses to lie for you — even
when you ask it to. Fill-only: it never submits.

**2/**
The landscape right now: apply-bots that blast generic resumes, invent
experience to match keywords, burn your accounts, and get you blacklisted
by recruiters who can smell the automation.

Speed was the selling point. Nobody asked what it was doing to your
reputation.

**3/**
So I built the one that says no.

It's an open-source MCP server: searches 4 job boards (public and official
APIs only), grills you on the
gaps in your background, tailors your resume, fills the forms — and runs
every application through a governance screen first.

**4/**
Three things it has refused to do, in testing:

❌ Invent "5 years of Kubernetes" because the job listing mentioned it
❌ Apply to a staff role I wasn't qualified for (score: 0.31, blocked)
❌ Click "submit" on any application form — fill-only, always.
   The human clicks submit.

**5/**
The part I'm proudest of: the grill.

Before it applies anywhere, it messages you — chat or WhatsApp —
with questions built from the actual gaps between your profile and the job.
No answers, no application. It's the conversation a good career coach would
force you to have.

**6/**
The boring machinery that makes it safe:

• Public and official APIs only — no scraping, no logged-in sessions
• Honest User-Agent, robots.txt obeyed fail-closed
• Max 5 applications/day, with a polite delay between requests
• Every block written to an audit ledger

**7/**
What it won't do, stated plainly:

No CAPTCHA bypass. No credential storage. No LinkedIn automation behind a
login — LinkedIn isn't in the product. Glassdoor isn't implemented either
— the stub says so honestly instead of pretending.

Constraints are the feature.

**8/**
It's open source. MCP-native, so it plugs into any agent that speaks the
protocol. Queue your applications, prep for interviews from your real
achievements — and let it say no when it should.

Repo: [link in bio]

**9/**
Genuine question for job seekers and hiring managers:

What should an AI *never* do in your name during a job search? I'm still
tuning the refusal list, and I'd rather hear it from you than discover it
the embarrassing way.

---

# LinkedIn version

Everyone is racing to build AI that applies to 1,000 jobs while you sleep.
I built the opposite — and I think the contrarian bet is the right one.

The current generation of apply-bots optimizes for volume: generic resumes,
keyword-stuffed experience, automated submits. It works until a recruiter
spots the pattern — and then it's your name on the burned account, not the
bot's.

So I built an open-source MCP server that treats your reputation as the
constraint, not the throughput. It searches four job boards, then grills
you — over chat or WhatsApp — on the gaps between your background
and the role before anything moves forward. Every application passes a
governance screen: a cover-letter honesty check that blocks invented skills,
qualification scoring that blocks roles you're not ready for, and a strict
fill-only policy — automation fills the form, a human always clicks submit.
Five applications a day, maximum, with a polite delay between requests,
with every decision written to an audit ledger.

In testing it has refused to invent experience, refused an unqualified
application I explicitly requested, and refused to auto-submit a form. I
consider all three refusals features.

If you're job hunting and want leverage without the reputational risk — or
you're hiring and have opinions on what automation should never do in a
candidate's name — I'd genuinely like to hear from you. Link in comments.
