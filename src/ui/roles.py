"""Role metadata shared across the UI pages."""

ROLE_LABELS: dict[str, str] = {
    "engineer": "🛠 Engineer",
    "team_lead": "👔 Team Lead",
    "manager": "📊 Manager",
}

ROLES = [
    {
        "id": "engineer",
        "title": "Engineer",
        "description": "Debug code, review designs, and work through technical problems.",
    },
    {
        "id": "team_lead",
        "title": "Team Lead",
        "description": "Plan sprints, unblock your team, and prepare 1:1s.",
    },
    {
        "id": "manager",
        "title": "Manager",
        "description": "Summarize progress, draft updates, and weigh decisions.",
    },
]
