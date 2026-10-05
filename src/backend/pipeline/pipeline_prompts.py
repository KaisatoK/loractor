from langchain_typesafe import Choice, Noul
from src.backend.pipeline.graph import PipelineState, PlanStep, Role

class PipelinePrompts:
    """Build the prompts used by each stage of the pipeline."""

    def role_text(self, role: Role) -> str:
        return f"Role: {role['name']}\nRole objective: {role['objective']}"

    def normalize(self, state: "PipelineState") -> str:
        return (
            f"{self.role_text(state['role'])}\n"
            f"User question: {state['question']}\n\n"
            "Classify the user's intent and extract the relevant parameters."
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
            f"User question: {state['question']}\n"
            f"Normalized request: {state['normalized']}\n"
            f"Previous plan: {previous_plan}\n"
            f"Previous plan history: {plan_history}\n"
            f"Revision feedback: {revision_feedback}\n\n"
            "Create a plan containing every tool call needed to gather sufficient "
            "evidence. Use multiple steps when necessary. Each step must name one "
            "available tool and provide its exact instruction. If this is a revision, "
            "change the approach to address the feedback; do not repeat the previous "
            "plan unchanged."
        )

    def execute_tool(self, state: "PipelineState", step: "PlanStep") -> str:
        return (
            f"Original question: {state['question']}\n"
            f"Normalized request: {state['normalized']}\n"
            f"Planned tool: {step['tool']}\n"
            f"Instruction: {step['instruction']}"
        )

    def answer(self, state: "PipelineState") -> str:
        return (
            f"{self.role_text(state['role'])}\n"
            f"User question: {state['question']}\n"
            f"Evidence: {state['evidence']}\n\n"
            "Write a grounded answer. Do not make claims that are not supported "
            "by the evidence. Tailor the explanation to the user's role."
        )

    def confidence_request(self, state: "PipelineState") -> dict:
        role_guidance = self.role_guidance(state["role"])
        return {
            "state": {
                "user_role": state["role"]["name"],
                "role_objective": state["role"]["objective"],
                "user_question": state["question"],
                "planned_steps": state["plan"],
                "found_evidence": state["evidence"],
                "answer": state.get("answer", ""),
            },
            "question": {
                "pass": self.pass_question(role_guidance),
                "next_state": self.next_state_question(role_guidance),
                "revision_reason": self.revision_reason_question(),
            },
        }

    def role_guidance(self, role: Role) -> str:
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
                    "evidence": "The evidence is relevant, specific, and sufficient to support the answer.",
                    "answer": "The answer is grounded in the evidence and explains its conclusions reliably.",
                },
                "false": {
                    "role_fit": "The answer is aimed at the wrong goals, decisions, responsibilities, or level of detail for the user's role.",
                    "evidence": "The evidence is missing, irrelevant, contradictory, or insufficient.",
                    "answer": "The answer is missing, unsupported by the evidence, or unreliable.",
                },
            },
        )

    def next_state_question(self, role_guidance: str) -> Choice:
        return Choice(
            instructions=(
                "Choose the next pipeline state. Select plan when more or better "
                "evidence is needed. Select answer when the evidence is sufficient "
                "but the answer must be generated or revised. Select conclude only "
                "when the answer is supported and ready to return.\n\n"
                f"Role objective: {role_guidance}"
            ),
            criteria={
                "plan": "Gather more evidence or revise the plan because the current evidence does not sufficiently answer the question.",
                "answer": "Use the current evidence to generate or revise an answer grounded in the evidence and useful for the user's inferred goals, decisions, responsibilities, and required level of detail.",
                "conclude": "The evidence and answer are sufficient, grounded, and useful for the user's inferred goals, decisions, responsibilities, and required level of detail.",
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
