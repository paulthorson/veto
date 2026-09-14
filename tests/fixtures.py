"""Realistic sample payloads for provider parser unit tests.

These mirror the real public API shapes of Greenhouse, Lever and Ashby,
and the LinkedIn guest-API HTML card, so parser tests exercise realistic
input without any network access.
"""

GREENHOUSE_JOB = {
    "id": 5432109,
    "title": "Senior Software Engineer, Backend",
    "location": {"name": "New York, NY"},
    "absolute_url": "https://boards.greenhouse.io/stripe/jobs/5432109",
    "metadata": [
        {"id": 123, "name": "Department", "value": "Engineering"},
        {"id": 124, "name": "Location", "value": "New York"},
    ],
    "departments": [{"id": 1, "name": "Engineering"}],
    "offices": [{"id": 10, "name": "New York", "location": "New York, NY"}],
    "content": "&lt;p&gt;Build payment infrastructure.&lt;/p&gt;",
}

LEVER_POSTING = {
    "id": "a1b2c3d4-1111-2222-3333-444455556666",
    "text": "Software Engineer",
    "categories": {
        "location": "New York",
        "team": "Engineering",
        "commitment": "Full-time",
    },
    "hostedUrl": "https://jobs.lever.co/acme/a1b2c3d4-1111-2222-3333-444455556666",
    "description": "Acme is hiring a software engineer...",
    "descriptionPlain": "Acme is hiring a software engineer...",
    "lists": [{"text": "What you'll do", "content": "Ship features."}],
}

ASHBY_POSTING = {
    "id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
    "title": "Frontend Engineer",
    "departmentName": "Engineering",
    "locationName": "New York, NY",
    "jobUrl": "https://jobs.ashbyhq.com/acme/f47ac10b-58cc-4372-a567-0e02b2c3d479",
    "isRemote": False,
    "compensationTierSummary": "$150k - $190k",
    "descriptionHtml": "<p>Build delightful UIs.</p>",
}


# The shape every provider's search() must return per job.
REQUIRED_JOB_KEYS = {"id", "title", "company", "location", "url", "board", "snippet"}
