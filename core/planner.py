"""Planner layer: turn a Goal into one or more candidate Plans.

The Kernel only knows the :class:`Planner` interface, so new planners (LLM
backed, search based, ...) can be dropped in without touching the kernel.
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional

from .models import Action, Goal, Plan


class PlanningError(Exception):
    """Raised when a goal cannot be turned into any plan."""


class Planner(ABC):
    """Abstract planner contract."""

    @abstractmethod
    def propose(self, goal: Goal) -> List[Plan]:
        """Return candidate plans, best first (index 0 == preferred)."""


class SequentialPlanner(Planner):
    """Deterministic planner that emits ordered, single-capability sequences.

    Unlike v0.1 it also produces *alternative* plans, so the kernel can fall
    back to a different strategy when policy rejects or execution fails.
    """

    def __init__(self, max_alternatives: int = 3) -> None:
        self.max_alternatives = max(1, int(max_alternatives))
        self._builders: Dict[str, Callable[[Goal], List[List[Action]]]] = {
            "create_file": self._create_file,
            "read_file": self._read_file,
            "append_file": self._append_file,
            "delete_file": self._delete_file,
            "list_dir": self._list_dir,
            "ensure_dir": self._ensure_dir,
        }

    # -- Planner interface -------------------------------------------------- #
    @property
    def supported_goal_types(self) -> List[str]:
        return sorted(self._builders)

    def propose(self, goal: Goal) -> List[Plan]:
        builder = self._builders.get(goal.goal_type)
        if builder is None:
            raise PlanningError(
                f"unsupported goal_type={goal.goal_type!r}; "
                f"supported={self.supported_goal_types}"
            )
        candidates = builder(goal)[: self.max_alternatives]
        if not candidates:
            raise PlanningError(f"no plan could be built for goal={goal.goal_id}")

        plans: List[Plan] = []
        for rank, actions in enumerate(candidates):
            plans.append(
                Plan(
                    goal_id=goal.goal_id,
                    actions=actions,
                    rationale=self._rationale(goal, rank, len(actions)),
                    estimated_cost=self._cost(actions, rank),
                )
            )
        return plans

    # -- goal -> candidate action sequences --------------------------------- #
    def _create_file(self, goal: Goal) -> List[List[Action]]:
        path = self._require(goal, "path")
        content = goal.params.get("content", "")
        parent = os.path.dirname(path) or "."
        candidates: List[List[Action]] = [
            [self._action("filesystem.write", {"path": path, "content": content},
                          "write the file directly (parents assumed to exist)")],
        ]
        if parent not in ("", ".", "/"):
            candidates.append(
                [
                    self._action("filesystem.mkdir", {"path": parent},
                                 "ensure the parent directory exists first"),
                    self._action("filesystem.write", {"path": path, "content": content},
                                 "write the file after the parent is guaranteed"),
                ]
            )
        candidates.append(
            [
                self._action("filesystem.delete", {"path": path, "missing_ok": True},
                             "remove any stale copy before rewriting"),
                self._action("filesystem.write", {"path": path, "content": content},
                             "write a clean copy of the file"),
            ]
        )
        return candidates

    def _read_file(self, goal: Goal) -> List[List[Action]]:
        path = self._require(goal, "path")
        return [
            [self._action("filesystem.read", {"path": path}, "read the file contents")]
        ]

    def _append_file(self, goal: Goal) -> List[List[Action]]:
        path = self._require(goal, "path")
        content = goal.params.get("content", "")
        return [
            [
                self._action("filesystem.read", {"path": path, "missing_ok": True},
                             "read the current contents"),
                self._action("filesystem.write",
                             {"path": path, "content": content, "append": True},
                             "append to the file"),
            ]
        ]

    def _delete_file(self, goal: Goal) -> List[List[Action]]:
        path = self._require(goal, "path")
        return [
            [
                self._action("filesystem.read", {"path": path},
                             "confirm the target exists before removing it"),
                self._action("filesystem.delete", {"path": path},
                             "delete the confirmed target"),
            ],
            [self._action("filesystem.delete", {"path": path, "missing_ok": True},
                          "delete unconditionally")],
        ]

    def _list_dir(self, goal: Goal) -> List[List[Action]]:
        path = goal.params.get("path", ".")
        return [
            [self._action("filesystem.list", {"path": path}, "list the directory")]
        ]

    def _ensure_dir(self, goal: Goal) -> List[List[Action]]:
        path = self._require(goal, "path")
        return [
            [self._action("filesystem.mkdir", {"path": path}, "create the directory")]
        ]

    # -- helpers ------------------------------------------------------------ #
    @staticmethod
    def _require(goal: Goal, key: str) -> Any:
        if key not in goal.params:
            raise PlanningError(
                f"goal_type={goal.goal_type!r} requires param {key!r}"
            )
        return goal.params[key]

    def _action(self, capability: str, params: Dict[str, Any], rationale: str) -> Action:
        return Action(
            capability=capability,
            params=dict(params),
            rationale=rationale,
            predicted_risk=self._predict_risk(capability, params),
        )

    @staticmethod
    def _predict_risk(capability: str, params: Dict[str, Any]) -> float:
        """Cheap heuristic prior; :class:`PolicyEngine` computes the real score."""
        base = {
            "filesystem.read": 0.05,
            "filesystem.list": 0.05,
            "filesystem.mkdir": 0.15,
            "filesystem.write": 0.20,
            "filesystem.delete": 0.45,
        }.get(capability, 0.30)
        size = 0
        content = params.get("content")
        if isinstance(content, str):
            size = len(content)
        elif content is not None:
            try:
                size = len(json.dumps(content))
            except (TypeError, ValueError):
                size = 0
        return round(min(base + size / 1_000_000.0, 1.0), 4)

    @staticmethod
    def _cost(actions: List[Action], rank: int) -> float:
        """Lower is better: fewer steps, less risk, earlier candidate wins ties."""
        return round(len(actions) + rank * 0.1 + sum(a.predicted_risk for a in actions), 4)

    @staticmethod
    def _rationale(goal: Goal, rank: int, steps: int) -> str:
        if rank == 0:
            return f"primary strategy for {goal.goal_type} in {steps} step(s)"
        return f"alternative #{rank} strategy for {goal.goal_type} in {steps} step(s)"


__all__ = ["Planner", "PlanningError", "SequentialPlanner"]
