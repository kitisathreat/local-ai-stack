"""
title: Ask Clarification — Present Multiple-Choice Questions to the User
author: local-ai-stack
description: Lets the model pause and ask the user a structured clarifying question before attempting a task. Renders a formatted multiple-choice list with an optional free-text invite. Use this when a request is ambiguous, could be interpreted multiple ways, or needs scoping before you begin. The user replies with a number to pick an option, or types a custom answer.
required_open_webui_version: 0.4.0
version: 1.0.0
licence: MIT
"""

from typing import Callable, Any, Optional
from pydantic import BaseModel, Field


ICONS = {
    "question": "🤔",
    "options": ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣"],
    "custom": "✏️",
    "hint": "💡",
}


class Tools:
    class Valves(BaseModel):
        SHOW_ICONS: bool = Field(
            default=True,
            description="Show emoji icons next to options (disable for plain-text environments)",
        )
        MAX_OPTIONS: int = Field(
            default=6,
            description="Maximum number of options to display (extra options are truncated)",
        )

    def __init__(self):
        self.valves = self.Valves()

    async def ask_user(
        self,
        question: str,
        options: str,
        context: str = "",
        allow_freetext: bool = True,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Ask the user a clarifying multiple-choice question before continuing with a task. Call this whenever a request is vague, could go in multiple directions, or needs scoping.
        :param question: The clarifying question to ask (e.g. 'What format should the output be?', 'Which time period are you interested in?')
        :param options: Pipe-separated list of concrete choices (e.g. 'PDF report|Interactive dashboard|Raw CSV data|Plain text summary'). Provide 2–6 options.
        :param context: Optional one-sentence explanation of why this clarification is needed (shown in italics above the question)
        :param allow_freetext: If True (default), invite the user to type a custom answer not in the list
        :return: Formatted multiple-choice question rendered in chat — wait for the user's reply before proceeding
        """
        option_list = [o.strip() for o in options.split("|") if o.strip()]
        option_list = option_list[: self.valves.MAX_OPTIONS]

        use_icons = self.valves.SHOW_ICONS
        q_icon = ICONS["question"] + " " if use_icons else ""
        custom_icon = ICONS["custom"] + " " if use_icons else ""

        lines = ["\n---\n"]

        if context.strip():
            lines.append(f"_{context.strip()}_\n")

        lines.append(f"{q_icon}**{question.strip()}**\n")

        for i, opt in enumerate(option_list):
            num_icon = (ICONS["options"][i] + " ") if use_icons and i < len(ICONS["options"]) else f"{i+1}. "
            lines.append(f">{num_icon} {opt}")

        if allow_freetext:
            lines.append(f"\n>{custom_icon}**Custom:** Type your own answer")

        lines.append("\n_Reply with the number of your choice, or type a custom answer._\n")
        lines.append("---\n")

        return "\n".join(lines)

    async def ask_yes_no(
        self,
        question: str,
        context: str = "",
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Ask the user a simple yes/no question before proceeding.
        :param question: The yes/no question (e.g. 'Should I include historical comparisons?')
        :param context: Optional brief explanation of why this matters
        :return: Formatted yes/no question — wait for the user's reply
        """
        return await self.ask_user(
            question=question,
            options="Yes|No",
            context=context,
            allow_freetext=False,
            __event_emitter__=__event_emitter__,
            __user__=__user__,
        )

    async def ask_scope(
        self,
        task_description: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Ask the user to clarify the scope/depth of a task — quick overview vs. deep dive vs. specific focus.
        :param task_description: Brief description of the task being scoped (e.g. 'financial analysis of AAPL')
        :return: Formatted scope question
        """
        return await self.ask_user(
            question=f"How in-depth should I go with the {task_description}?",
            options=(
                "Quick summary (key points only)|"
                "Standard overview (balanced detail)|"
                "Deep dive (comprehensive analysis)|"
                "Specific section only (tell me which)"
            ),
            context="Different levels of depth suit different needs — let me know what's most useful.",
            allow_freetext=True,
            __event_emitter__=__event_emitter__,
            __user__=__user__,
        )
