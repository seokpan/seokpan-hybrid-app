"""Deterministic in-memory implementation of the Game persistence port."""

from __future__ import annotations

from seokpan.game.application import (
    FinalizeGameCommand,
    GamePersistenceSnapshot,
    OfficialMoveRecord,
    PersistenceOutcome,
    PersistenceRuleViolation,
    StartGameCommand,
    StoredGameResult,
)
from seokpan.game.domain import GameParticipantRole, GameParticipantSnapshot


class InMemoryGamePersistenceAdapter:
    """A Headless fake; passing it is not MariaDB integration evidence."""

    def __init__(self, member_ratings: dict[int, int] | None = None) -> None:
        self.games: dict[str, StartGameCommand] = {}
        self.moves: dict[tuple[str, int], OfficialMoveRecord] = {}
        self.results: dict[str, FinalizeGameCommand] = {}
        self.member_ratings = {} if member_ratings is None else member_ratings

    async def start_game(self, command: StartGameCommand) -> PersistenceOutcome:
        existing = self.games.get(command.game_id)
        if existing is None:
            self.games[command.game_id] = command
            return PersistenceOutcome.CREATED
        if existing == command:
            return PersistenceOutcome.UNCHANGED
        raise PersistenceRuleViolation("GAME_START_CONFLICT")

    async def append_move(self, command: OfficialMoveRecord) -> PersistenceOutcome:
        if command.game_id not in self.games:
            raise PersistenceRuleViolation("GAME_NOT_FOUND")
        key = (command.game_id, command.move_no)
        by_turn = await self.get_move(command.game_id, command.turn_no)
        existing = by_turn if by_turn is not None else self.moves.get(key)
        if existing is None:
            self.moves[key] = command
            return PersistenceOutcome.CREATED
        if existing == command:
            return PersistenceOutcome.UNCHANGED
        raise PersistenceRuleViolation("MOVE_SEQUENCE_CONFLICT")

    async def finalize_game(self, command: FinalizeGameCommand) -> PersistenceOutcome:
        game_id = command.result.game_id
        if game_id not in self.games:
            raise PersistenceRuleViolation("GAME_NOT_FOUND")
        existing = self.results.get(game_id)
        if existing is None:
            # Validate every update before changing any shared Fake state.
            for adjustment in command.result.rating_adjustments:
                rating = self.member_ratings.get(adjustment.member_id)
                if rating is None:
                    raise PersistenceRuleViolation("MEMBER_NOT_FOUND")
                if rating != adjustment.rating_before:
                    raise PersistenceRuleViolation("STALE_MEMBER_RATING")
            for adjustment in command.result.rating_adjustments:
                self.member_ratings[adjustment.member_id] = adjustment.rating_after
            self.results[game_id] = command
            return PersistenceOutcome.CREATED
        if existing == command:
            return PersistenceOutcome.UNCHANGED
        raise PersistenceRuleViolation("GAME_RESULT_CONFLICT")

    async def load_game(self, game_id: str) -> GamePersistenceSnapshot | None:
        start = self.games.get(game_id)
        if start is None:
            return None
        prior_ratings = {
            item.member_id: item.rating_before
            for item in (
                ()
                if game_id not in self.results
                else self.results[game_id].result.rating_adjustments
            )
        }
        participants: list[GameParticipantSnapshot] = []
        for item in start.participants:
            rating = None
            if item.member_id is not None:
                rating = prior_ratings.get(item.member_id, self.member_ratings.get(item.member_id))
                if rating is None:
                    raise PersistenceRuleViolation("MEMBER_RATING_NOT_FOUND")
            participants.append(
                GameParticipantSnapshot(
                    participant_id=item.participant_id,
                    team=item.team,
                    role=GameParticipantRole.PLAYER,
                    member_id=item.member_id,
                    rating=rating,
                )
            )
        moves = tuple(
            value
            for key, value in sorted(self.moves.items(), key=lambda item: item[0][1])
            if key[0] == game_id
        )
        return GamePersistenceSnapshot(start, tuple(participants), moves)

    async def load_result(self, game_id: str) -> StoredGameResult | None:
        command = self.results.get(game_id)
        if command is None:
            return None
        start = self.games.get(game_id)
        if start is None:
            raise PersistenceRuleViolation("GAME_RESULT_INCOMPLETE")
        result = command.result
        if result.game_id != game_id:
            raise PersistenceRuleViolation("GAME_RESULT_INCOMPLETE")
        expected_members = {
            (item.participant_id, item.member_id, item.team)
            for item in start.participants
            if item.member_id is not None and result.stats_eligible
        }
        actual_members = {
            (item.participant_id, item.member_id, item.team) for item in result.rating_adjustments
        }
        if expected_members != actual_members:
            raise PersistenceRuleViolation("GAME_RESULT_INCOMPLETE")
        return StoredGameResult(
            game_id,
            start.room_id,
            result.status,
            result.end_reason,
            result.winner,
            command.ended_at,
            tuple(sorted(result.rating_adjustments, key=lambda item: item.member_id)),
        )

    async def get_move(self, game_id: str, turn_no: int) -> OfficialMoveRecord | None:
        for move in self.moves.values():
            if move.game_id == game_id and move.turn_no == turn_no:
                return move
        return None

    async def result_matches(self, command: FinalizeGameCommand) -> bool:
        existing = self.results.get(command.result.game_id)
        if existing is None:
            return False
        if existing == command:
            return True
        raise PersistenceRuleViolation("GAME_RESULT_CONFLICT")

    async def game_is_finalized(self, game_id: str) -> bool:
        return game_id in self.results
