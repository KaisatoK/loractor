from langchain_typesafe import Choice, ClassifierRequest, Noul, NoulCriteria
from backend.pipeline.schemas import PipelineState, Role
from backend.pipeline.utils import LOG_SCHEMA, TOOL_SCHEMA


class PipelinePrompts:
    """Build the prompts used by each stage of the pipeline."""

    shared_grounding = (
        "Use only observed tool results. Separate evidence from interpretation. "
        "Do not invent facts, causes, impact, or missing details. Treat supported "
        "hypotheses as findings, contradicted hypotheses as excluded, and unresolved "
        "hypotheses as limitations. Include relevant time ranges, filters, calculations, "
        "evidence references, and confidence."
    )

    def role_context(self, role: Role) -> str:
        return f"User role: {role['name']}\n" f"Role objective: {role['objective']}\n\n"

    def scope_request(self, state: PipelineState) -> ClassifierRequest:
        return {
            "state": {
                "user_message": state["question"],
            },
            "questions": {
                "route": self.scope_question(),
            },
        }

    def scope_question(self) -> Noul:
        return Noul(
            instructions=(
                "Determine whether the user message requires investigation using "
                "log data."
            ),
            criteria=NoulCriteria(
                true=(
                    "The user asks about logs, errors, failures, incidents, traces, "
                    "traces, recurring operational patterns, system impact, or priorities "
                    "that require log evidence."
                ),
                false=(
                    "The message is a greeting, thanks, casual conversation, general "
                    "help request, or is unrelated to log investigation."
                ),
            ),
        )

    def normalize(self, state: PipelineState) -> str:
        return (
            f"User question: {state['question']}\n\n"
            "Normalize the user's question into a concise, structured format."
        )

    def plan(self, state: PipelineState) -> str:
        previous_plan = state.get("plan") or "None"
        revision_feedback = state.get(
            "revision_feedback",
            "No previous attempt exists.",
        )
        plan_history = state.get("plan_history", [])

        return (
            f"{self.role_context(state['role'])}\n"
            f"Normalized request: {state['normalized']}\n"
            f"Log table schema:\n{LOG_SCHEMA}\n"
            f"Previous plan: {previous_plan}\n"
            f"Previous attempt history, including evidence statuses: {plan_history}\n"
            f"Revision feedback: {revision_feedback}\n\n"
            "Create an investigation plan containing the plausible hypotheses that "
            "could explain the problem. For each hypothesis, describe why it matters "
            "and list the specific evidence required to evaluate it. Do not name tools "
            "or assume knowledge of the available tool set. If this is a revision, "
            "change the hypotheses or evidence requirements to address the feedback; "
            "do not repeat the previous plan unchanged."
        )

    def execute_evidence(self, state: PipelineState) -> str:
        return (
            f"Normalized request: {state['normalized']}\n"
            f"Log table schema:\n{LOG_SCHEMA}\n"
            f"Investigation plan: {state['plan']}\n\n"
            f"Tool schemas:\n{TOOL_SCHEMA}\n\n"
            "Collect the required evidence for each hypothesis using the available "
            "tools. Return only evidence that appears in tool results. If evidence "
            "cannot be collected, mark it as unavailable rather than guessing."
        )

    def answer(self, state: PipelineState) -> str:
        previous_confidence = (
            state.get("confidence") or "No previous confidence evaluation."
        )
        revision_feedback = state.get(
            "revision_feedback",
            "No answer revision feedback is available.",
        )
        return (
            f"{self.role_context(state['role'])}\n"
            f"Normalized request: {state['normalized']}\n"
            f"Investigation hypotheses: {state['plan']}\n"
            f"Evidence: {state['evidence']}\n\n"
            f"Previous confidence evaluation: {previous_confidence}\n"
            f"Required revision: {revision_feedback}\n\n"
            "Answer the user's question directly. Focus on supported hypotheses, exclude "
            "contradicted hypotheses, and mention unresolved hypotheses only when they "
            "materially limit the conclusion. Tailor the detail and recommendation to the "
            "user's role. Apply the revision feedback.\n\n"
            f"Grounding rules: {self.shared_grounding}"
        )

    # def confidence_request(self, state: PipelineState) -> ClassifierRequest:
    #     role_guidance = self.role_guidance(state["role"])
    #     return {
    #         "state": {
    #             "user_role": state["role"]["name"],
    #             "role_objective": state["role"]["objective"],
    #             "user_question": state["question"],
    #             "hypotheses": state["plan"],
    #             "evidence_report": state["evidence"],
    #             "answer": state.get("answer", ""),
    #         },
    #         "question": {
    #             "pass": self.pass_question(role_guidance),
    #             "next_state": self.next_state_question(role_guidance),
    #             "revision_reason": self.revision_reason_question(),
    #         },
    #     }

    def pass_question(self, role: Role) -> Noul:
        return Noul(
            instructions=(
                "Estimate whether the current evidence and answer are sufficient "
                "to answer the user's question accurately and for the user's role.\n\n"
                f"{self.role_context(role)}"
            ),
            criteria=NoulCriteria(
                true={
                    "role_fit": "The answer supports the role objective at the appropriate level of detail.",
                    "grounding": "Material claims and hypothesis statuses are supported by tool results.",
                    "completeness": "The answer resolves the question or clearly states its limitations.",
                },
                false={
                    "role_fit": "The answer does not support the role objective.",
                    "grounding": "The answer contains unsupported claims or misrepresents evidence.",
                    "completeness": "Important findings or limitations are missing.",
                },
            ),
        )

    def next_state_question(self, role: Role) -> Choice:
        return Choice(
            instructions=(
                "Choose the next pipeline state based on evidence sufficiency and "
                "answer quality.\n\n"
                f"{self.role_context(role)}"
            ),
            criteria={
                "plan": (
                    "Evidence is missing, unavailable, irrelevant, contradictory, "
                    "or no important hypothesis is supported."
                ),
                "answer": (
                    "Evidence is sufficient, but the answer must be generated or revised."
                ),
                "conclude": (
                    "The answer is grounded, role-appropriate, and ready to return."
                ),
            },
        )

    def revision_reason_question(self) -> Choice:
        return Choice(
            instructions=(
                "Identify the main reason the current attempt should be revised. "
                "Choose none only when the evidence and answer are ready to return."
            ),
            criteria={
                "missing_evidence": "More evidence is required.",
                "irrelevant_evidence": "The evidence does not address the important hypotheses.",
                "unsupported_answer": "The evidence is adequate, but the answer misuses or omits it.",
                "role_mismatch": "The answer does not support the user's role objective.",
                "none": "The result is grounded and ready to return.",
            },
        )
