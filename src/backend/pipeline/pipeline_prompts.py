from langchain_typesafe import Choice, Noul
from src.backend.pipeline.schemas import PipelineState, Role

class PipelinePrompts:
    """Build the prompts used by each stage of the pipeline."""

    def role_text(self, role: "Role") -> str:
        return f"Role: {role['name']}\nRole objective: {role['objective']}"

    def normalize(self, state: "PipelineState") -> str:
        return (
            f"User question: {state['question']}\n\n"
            "Normalize the user's question into a concise, structured format."
        )

    def plan(self, state: "PipelineState") -> str:
        previous_plan = state.get("plan") or "None"
        revision_feedback = state.get(
            "revision_feedback",
            "No previous attempt exists.",
        )
        plan_history = state.get("plan_history", [])

        return (
            f"{self.role_text(state['role'])}\n"
            f"Normalized request: {state['normalized']}\n"
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

    def execute_evidence(self, state: "PipelineState") -> str:
        return (
            f"Normalized request: {state['normalized']}\n"
            f"Investigation plan: {state['plan']}\n\n"
            "Collect the required evidence for each hypothesis using the available "
            "tools. Return only evidence that appears in tool results. If evidence "
            "cannot be collected, mark it as unavailable rather than guessing."
        )

    def answer(self, state: "PipelineState") -> str:
        previous_confidence = state.get("confidence") or "No previous confidence evaluation."
        revision_feedback = state.get(
            "revision_feedback",
            "No answer revision feedback is available.",
        )
        return (
            f"{self.role_text(state['role'])}\n"
            f"Normalized request: {state['normalized']}\n"
            f"Investigation hypotheses: {state['plan']}\n"
            f"Evidence: {state['evidence']}\n\n"
            f"Previous confidence evaluation: {previous_confidence}\n"
            f"Required revision: {revision_feedback}\n\n"
            "Write the answer to the user's problem, not a list of all hypotheses. "
            "Use only hypotheses marked supported when explaining the likely cause or "
            "answer. Ignore hypotheses marked contradicted; do not present them as "
            "possible causes. Mention unresolved hypotheses only when the missing or "
            "unavailable evidence materially limits the answer. Never turn not_found "
            "or unavailable evidence into a fact, and never make claims not supported "
            "by raw tool results. Apply the required revision explicitly. Tailor the "
            "answer to the user's role."
        )

    def confidence_request(self, state: "PipelineState") -> dict:
        role_guidance = self.role_guidance(state["role"])
        return {
            "state": {
                "user_role": state["role"]["name"],
                "role_objective": state["role"]["objective"],
                "user_question": state["question"],
                "hypotheses": state["plan"],
                "evidence_report": state["evidence"],
                "answer": state.get("answer", ""),
            },
            "question": {
                "pass": self.pass_question(role_guidance),
                "next_state": self.next_state_question(role_guidance),
                "revision_reason": self.revision_reason_question(),
            },
        }

    def role_guidance(self, role: "Role") -> str:
        return (
            f"The user's role is {role['name']}. Their objective is: "
            f"{role['objective']}"
        )

    def pass_question(self, role_guidance: str) -> Noul:
        return Noul(
            instructions=(
                "Estimate whether the current evidence and answer are sufficient "
                "to answer the user's question accurately and for the user's role.\n\n"
                f"Role objective: {role_guidance}"
            ),
            criteria={
                "true": {
                    "role_fit": "The answer is useful for the user's inferred goals, responsibilities, decisions, and level of detail.",
                    "evidence": "The evidence statuses and hypothesis statuses are supported by tool results.",
                    "answer": "The answer focuses on supported hypotheses, excludes contradicted hypotheses, and discloses unresolved hypotheses only when they limit the conclusion.",
                },
                "false": {
                    "role_fit": "The answer is aimed at the wrong goals, decisions, responsibilities, or level of detail for the user's role.",
                    "evidence": "The evidence statuses or hypothesis statuses are unsupported, incomplete, or inconsistent with the tool results.",
                    "answer": "The answer includes contradicted hypotheses, treats unresolved evidence as fact, or fails to focus on supported hypotheses.",
                },
            },
        )

    def next_state_question(self, role_guidance: str) -> Choice:
        return Choice(
            instructions=(
                "Choose the next pipeline state using the hypotheses, required evidence, "
                "evidence statuses, and answer. Select plan when an important hypothesis "
                "has missing, unavailable, irrelevant, or contradictory evidence. Select "
                "answer when the evidence is sufficient but the answer must be generated "
                "or revised. Select conclude only when every important hypothesis has "
                "been evaluated and the answer is grounded in observed tool results.\n\n"
                f"Role objective: {role_guidance}"
            ),
            criteria={
                "plan": "Re-plan because no hypothesis is supported, or an important hypothesis has missing, unavailable, irrelevant, or contradictory evidence.",
                "answer": "Use the evidence to generate or revise an answer that focuses on supported hypotheses, excludes contradicted hypotheses, and clearly limits unresolved conclusions.",
                "conclude": "The answer focuses on supported hypotheses, excludes contradicted hypotheses, treats unresolved hypotheses as limitations rather than facts, and is grounded in observed tool results.",
            },
        )

    def revision_reason_question(self) -> Choice:
        return Choice(
            instructions=(
                "Identify the main reason the current attempt should be revised. "
                "Choose none only when the evidence and answer are ready to return."
            ),
            criteria={
                "missing_evidence": "The plan did not collect enough evidence to answer the question.",
                "irrelevant_evidence": "The collected evidence does not address the question or is contradictory.",
                "unsupported_answer": "The evidence is useful, but the answer contains unsupported claims or omissions.",
                "role_mismatch": "The evidence and answer do not address the user's role or decision needs.",
                "none": "The evidence is relevant and sufficient, and the answer is grounded and ready to return.",
            },
        )
