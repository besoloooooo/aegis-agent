"""Team management — persistent teammates and inter-agent messaging.

A :class:`TeamManager` owns one or more teams.  Each team has a lead (the Main
Agent, by convention named ``team-lead``) and a set of named
:class:`~aegis_agent.agents.teammate.PersistentTeammate` members.  Messages
between agents travel over an :class:`~aegis_agent.agents.messaging.AgentTransport`
(the in-process default), so the team layer never touches a concrete queue or
file.

This is the Aegis analogue of Claude Code's TeamCreate + SendMessage routing +
in-process teammate supervision, adapted to Aegis's dependency-injected runtime
and thread-based model.  Team boundary: a sender may only address members of
its own team (by name), or broadcast to ``*`` within that team — there is no
cross-team delivery.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field

from aegis_agent.agents.definitions import AgentDefinition
from aegis_agent.agents.messaging import AgentMessage, AgentTransport, MessageType
from aegis_agent.agents.runner import SubagentRunner, SubagentStatus
from aegis_agent.agents.teammate import PersistentTeammate

#: Conventional name of the team's lead agent (the Main Agent).
LEAD_NAME = "team-lead"


@dataclass
class TeamMember:
    """A member entry in a team roster (lead or teammate)."""

    name: str
    agent_id: str
    agent_type: str
    status: str
    is_lead: bool = False


@dataclass
class Team:
    """A team: an id, a lead, and its teammate members."""

    team_id: str
    description: str = ""
    lead: str = LEAD_NAME
    teammates: dict[str, PersistentTeammate] = field(default_factory=dict)


class TeamManager:
    """Create teams, spawn persistent teammates, and route messages."""

    def __init__(
        self,
        runner: SubagentRunner,
        transport: AgentTransport,
        agents: dict[str, AgentDefinition],
    ) -> None:
        self._runner = runner
        self._transport = transport
        self._agents = dict(agents)
        self._lock = threading.Lock()
        self._teams: dict[str, Team] = {}
        # agent_id → (team_id, teammate) for O(1) lookup by id.
        self._by_agent_id: dict[str, tuple[str, PersistentTeammate]] = {}
        # The team the lead (Main Agent) most recently created.  The lead's own
        # send_message is unbound (it doesn't know the team id ahead of time) and
        # resolves through this.  Single-lead-team in v1.
        self._lead_team_id: str | None = None

    # -- team lifecycle ----------------------------------------------------

    def create_team(self, description: str = "", team_id: str | None = None) -> Team:
        """Create a team and return it.  The lead is the Main Agent."""
        with self._lock:
            tid = team_id or f"team-{uuid.uuid4().hex[:8]}"
            if tid in self._teams:
                raise ValueError(f"team already exists: {tid}")
            team = Team(team_id=tid, description=description)
            self._teams[tid] = team
            self._lead_team_id = tid
            return team

    def lead_team_id(self) -> str | None:
        """The lead's active team id, or ``None`` if no team exists yet."""
        with self._lock:
            return self._lead_team_id

    def get_team(self, team_id: str) -> Team | None:
        with self._lock:
            return self._teams.get(team_id)

    def list_members(self, team_id: str) -> list[TeamMember]:
        """Roster snapshot: the lead plus each teammate (stable names)."""
        with self._lock:
            team = self._teams.get(team_id)
            if team is None:
                return []
            members = [
                TeamMember(
                    name=team.lead,
                    agent_id=f"{team_id}/{team.lead}",
                    agent_type="lead",
                    status="lead",
                    is_lead=True,
                )
            ]
            for tm in team.teammates.values():
                members.append(
                    TeamMember(
                        name=tm.name,
                        agent_id=tm.agent_id,
                        agent_type=tm.definition.name,
                        status=tm.status.value,
                    )
                )
            return members

    # -- spawning ----------------------------------------------------------

    def spawn_teammate(
        self,
        team_id: str,
        name: str,
        agent_type: str = "general-purpose",
        *,
        initial_task: str | None = None,
    ) -> PersistentTeammate:
        """Spawn a named, persistent teammate into a team.

        The teammate gets a stable ``name`` (used for messaging), its own
        persistent transcript, and starts on its own thread.  ``initial_task``
        (when given) is delivered as the lead's first message.  Raises
        ``ValueError`` on unknown team / duplicate name / unknown agent type.
        """
        with self._lock:
            team = self._teams.get(team_id)
            if team is None:
                raise ValueError(f"unknown team: {team_id}")
            if name == team.lead or name in team.teammates:
                raise ValueError(f"duplicate member name in team {team_id}: {name}")
        definition = self._agents.get(agent_type)
        if definition is None:
            raise ValueError(
                f"unknown agent type '{agent_type}'. Available: {', '.join(sorted(self._agents))}"
            )

        from aegis_agent.agents.team_tools import SendMessageTool

        send_tool = SendMessageTool(self, team_id, sender=name)
        teammate = PersistentTeammate(
            name=name,
            team_id=team_id,
            definition=definition,
            runner=self._runner,
            transport=self._transport,
            lead_id=team.lead,
            team_tools=[send_tool],
            on_idle=self._make_idle_hook(team_id),
        )
        with self._lock:
            team.teammates[name] = teammate
            self._by_agent_id[teammate.agent_id] = (team_id, teammate)

        initial = None
        if initial_task is not None:
            initial = AgentMessage(
                sender=team.lead,
                recipient=name,
                content=initial_task,
                type=MessageType.TASK,
            )
        teammate.start(initial)
        return teammate

    # -- messaging ---------------------------------------------------------

    def send_message(
        self,
        team_id: str,
        sender: str,
        recipient: str,
        content: str,
        *,
        type: MessageType = MessageType.MESSAGE,
    ) -> None:
        """Send a message within a team.  ``recipient="*"`` broadcasts.

        Enforces the team boundary: the recipient must be the lead or a member
        of *this* team.  Raises ``ValueError`` otherwise.
        """
        team = self.get_team(team_id)
        if team is None:
            raise ValueError(f"unknown team: {team_id}")

        if recipient == "*":
            for name in team.teammates:
                if name != sender:
                    self._deliver(team, sender, name, content, type)
            return

        self._require_member(team, recipient)
        self._deliver(team, sender, recipient, content, type)

    def _deliver(self, team: Team, sender: str, recipient: str, content: str, type) -> None:
        self._transport.send(
            AgentMessage(sender=sender, recipient=recipient, content=content, type=type)
        )

    def _require_member(self, team: Team, name: str) -> None:
        if name != team.lead and name not in team.teammates:
            raise ValueError(
                f"'{name}' is not a member of team {team.team_id}. "
                f"Members: {team.lead}, {', '.join(sorted(team.teammates)) or '(none)'}"
            )

    # -- shutdown ----------------------------------------------------------

    def stop_teammate(self, team_id: str, name: str) -> bool:
        """Stop one teammate (its loop exits; resources released)."""
        team = self.get_team(team_id)
        if team is None:
            return False
        teammate = team.teammates.get(name)
        if teammate is None:
            return False
        teammate.stop()
        return True

    def stop_team(self, team_id: str) -> None:
        """Stop every teammate in a team and drop it from the registry."""
        with self._lock:
            team = self._teams.pop(team_id, None)
        if team is None:
            return
        for teammate in team.teammates.values():
            teammate.stop()
            self._by_agent_id.pop(teammate.agent_id, None)

    def resolve(self, team_id: str, name_or_id: str) -> PersistentTeammate | None:
        """Resolve a teammate by stable name or by agent_id (for messaging)."""
        team = self.get_team(team_id)
        if team is None:
            return None
        if name_or_id in team.teammates:
            return team.teammates[name_or_id]
        found = self._by_agent_id.get(name_or_id)
        return found[1] if found and found[0] == team_id else None

    # -- lead inbox ----------------------------------------------------------

    def drain_lead_messages(self, lead: str = LEAD_NAME) -> list[AgentMessage]:
        """Pop all messages waiting for the lead (called between Main turns).

        The lead is the Main Agent, not a thread — it has no loop to block on
        the transport.  Instead the runtime/CLI drains its inbox between turns
        and injects the messages as the next input, exactly like background
        subagent notifications (push model, no polling).
        """
        drained: list[AgentMessage] = []
        while self._transport.has_pending(lead):
            message = self._transport.receive(lead, timeout=0)
            if message is None:
                break
            drained.append(message)
        return drained

    # -- lead notification -------------------------------------------------

    def _make_idle_hook(self, team_id: str):
        """Build the per-teammate idle callback.

        When a teammate finishes a turn and goes IDLE, it drops a note into the
        lead's inbox so the lead learns of it (mirroring Claude's automatic
        idle notification).  Failure transitions include the error.
        """

        def _hook(teammate: PersistentTeammate, result) -> None:
            if result.status is SubagentStatus.FAILED:
                body = f"turn failed: {result.error or 'unknown error'}"
            else:
                body = "idle (available for work)"
            self._transport.send(
                AgentMessage(
                    sender=teammate.name,
                    recipient=LEAD_NAME,
                    content=f"[{teammate.name}] {body}",
                    type=MessageType.MESSAGE,
                )
            )

        return _hook


__all__ = ["LEAD_NAME", "Team", "TeamManager", "TeamMember"]
