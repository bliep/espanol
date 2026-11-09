"""Switches the session between lesson modes (set_mode RPC from the web client).

  conversation ──► Tutor          feedback panel on
  roleplay     ──► RolePlay       feedback panel on
  drills       ──► Drill agents   feedback panel paused (drills publish their own results)

Uses LiveKit's agent handoff (session.update_agent); the new agent speaks first.

Typed messages: LiveKit's default text input replies with the LLM directly, skipping
on_user_turn_completed, so typed drill answers are routed to the drill here.
"""

import logging

from livekit.agents import Agent, AgentSession, room_io

from drills import DRILLS, Drill, DrillDeps
from feedback_channel import FeedbackChannel
from modes import DRILL_MODES, LessonMode, ModeRequest
from turn_recorder import TurnRecorder
from tutor_agents import RESUME, RolePlay, Tutor

log = logging.getLogger("tutor.modes")

DEFAULT_TEXT_INPUT = room_io.TextInputOptions().text_input_cb  # LiveKit's own handler


class ModeController:
    def __init__(
        self,
        session: AgentSession,
        feedback: FeedbackChannel,
        deps: DrillDeps,
        recorder: TurnRecorder | None = None,
    ) -> None:
        self._session = session
        self._recorder = recorder
        self._feedback = feedback
        self._deps = deps
        self._current: ModeRequest = ModeRequest(mode=LessonMode.CONVERSATION)

    @property
    def current(self) -> ModeRequest:
        return self._current

    def set_mode(self, request: ModeRequest) -> None:
        if request == self._current:
            return

        self._current = request
        if request.mode in DRILL_MODES:
            self._feedback.pause()
        else:
            self._feedback.resume()

        self._session.update_agent(self._agent(request))
        log.info("mode %s", request.model_dump_json())

    async def on_text_input(self, session: AgentSession, ev: room_io.TextInputEvent) -> None:
        """RoomOptions text_input_cb: drills score typed answers, other modes reply as usual."""
        agent = session.current_agent
        if not isinstance(agent, Drill):
            await DEFAULT_TEXT_INPUT(session, ev)
            return

        log.info("typed drill answer: %s", ev.text)
        await session.interrupt()
        await agent.answer(ev.text)

    def _agent(self, request: ModeRequest) -> Agent:
        deps = self._deps
        match request.mode:
            case LessonMode.CONVERSATION:
                return Tutor(deps.level, deps.lesson, RESUME, self._recorder)
            case LessonMode.ROLEPLAY:
                return RolePlay(deps.level, deps.lesson, request.scenario, self._recorder)
            case _:
                return DRILLS[request.mode](deps)
