"""Open mic vs push-to-talk, switchable mid-session.

  OPEN  mic always live; the turn detector decides when the learner is done.
  PTT   mic muted until the learner holds the button:

        ptt_start ──► interrupt tutor, mic on ──► learner speaks
        ptt_end   ──► mic off, commit turn    ──► tutor replies
        ptt_cancel ─► mic off, drop turn      ──► nothing happens

Transport-agnostic: main.py maps room RPC calls onto these methods.
"""

import logging
from enum import StrEnum

from livekit.agents import AgentSession

log = logging.getLogger("tutor.input")

MANUAL = "manual"  # LiveKit turn_detection mode: turns end only on commit


class InputMode(StrEnum):
    OPEN = "open"
    PTT = "ptt"


class InputController:
    def __init__(self, session: AgentSession) -> None:
        self._session = session
        self._auto_detection = session.turn_detection  # restored when back to OPEN
        self._mode = InputMode.OPEN

    def set_mode(self, mode: InputMode) -> None:
        if mode == self._mode:
            return

        self._mode = mode
        if mode == InputMode.PTT:
            self._session.update_options(turn_detection=MANUAL)
            self._session.input.set_audio_enabled(False)
        else:
            self._session.clear_user_turn()
            self._session.update_options(turn_detection=self._auto_detection)
            self._session.input.set_audio_enabled(True)

        log.info("input mode %s", mode)

    def start_turn(self) -> None:
        if not self._ptt_active():
            return

        self._session.interrupt()  # barge-in: holding the button stops the tutor
        self._session.clear_user_turn()
        self._session.input.set_audio_enabled(True)

    def end_turn(self) -> None:
        if not self._ptt_active():
            return

        self._session.input.set_audio_enabled(False)
        self._session.commit_user_turn()

    def cancel_turn(self) -> None:
        if not self._ptt_active():
            return

        self._session.input.set_audio_enabled(False)
        self._session.clear_user_turn()

    def _ptt_active(self) -> bool:
        if self._mode == InputMode.PTT:
            return True

        log.warning("push-to-talk call ignored in %s mode", self._mode)
        return False
