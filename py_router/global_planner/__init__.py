"""CDT global-routing planner (Phase A standalone prototype).

Submodules:
- capacity_graph: Task 1 -- planar routing graph from constrained triangulation
- planner: Task 2 -- congestion-aware global routing of 2-pin nets
- validate: Task 3 -- validation against real routed copper
"""
from .capacity_graph import build_capacity_graph, CapacityGraph, Obstacle
from .planner import plan_board, PlanResult

__all__ = ["build_capacity_graph", "CapacityGraph", "Obstacle", "plan_board", "PlanResult"]
