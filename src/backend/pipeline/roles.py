"""Role objectives shared by the API request contract and the prompt builders."""

from backend.pipeline.state import RoleName

ROLE_OBJECTIVES: dict[RoleName, str] = {
    "engineer": (
        "Help the engineer identify the most likely cause of a specific error, inspect "
        "the supporting log evidence, and determine the next technical action required "
        "to resolve or further diagnose the issue. Answer specifically and technically. "
        "Be evidence-first, chronological, and traceable: include timestamps, trace IDs, "
        "request IDs, components, hosts, error codes, and log sources when available. "
        "Separate confirmed facts from likely causes and uncertainties. Structure the "
        "answer as: What happened; Most likely cause; Relevant evidence; Event timeline; "
        "Confidence and uncertainties; Recommended next debugging steps."
    ),
    "team_lead": (
        "Help the team lead understand recurring failures, identify patterns across "
        "components and time periods, assess operational impact, and determine which "
        "engineering improvement would prevent the most repeated incidents. Focus on "
        "aggregated, comparative, quantitative findings: counts, rates, trends, and "
        "affected users or requests when available. Prioritize recurring problems by "
        "frequency, severity, and impact, grouping symptoms only when the evidence "
        "supports it. Structure the answer as: Pattern summary; Top recurring issues; "
        "Trend and comparison; Affected systems or workflows; Likely contributing "
        "factors; Recommended team actions, including ownership and preventive work."
    ),
    "manager": (
        "Help the manager decide what to prioritize by translating technical findings "
        "into business impact, operational risk, urgency, and expected value of corrective "
        "action. Be concise and outcome-focused. Present a short ranked list based on "
        "impact, urgency, recurrence, effort, and confidence. Explain effects on "
        "customers, revenue, availability, productivity, or risk only when supported by "
        "the data. Structure the answer as: Executive summary; Business or operational "
        "impact; Prioritized issues; Reason for the ranking; Recommended decision; "
        "Success measures."
    ),
}
