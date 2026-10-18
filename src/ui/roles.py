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
        "description": "Investigate a specific error and what to check next.",
    },
    {
        "id": "team_lead",
        "title": "Team Lead",
        "description": "See which problems recur and which teams are involved.",
    },
    {
        "id": "manager",
        "title": "Manager",
        "description": "Rank problems by measured impact and decide what to act on.",
    },
]
