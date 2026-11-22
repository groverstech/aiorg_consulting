"""A deterministic stand-in for the language model, playing every agent in a synthetic business
scenario. It decides only what a model would decide (which tools to call, with what arguments, and
what to write); everything else -- runtime gates, permissions, budgets, the worker, the objective
coordinator -- is the real system.

This demonstrates the MECHANICS of autonomous operation. It says nothing about the quality of a
live model's decisions.
"""
from __future__ import annotations

import json
import re

from wwos.llm import LLMResult

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def _call(i, name, args):
    return {"type": "tool_use", "id": f"toolu_sim_{i}_{name}", "name": name.replace(".", "__"), "input": args}


class SimulatedOrganisation:
    name = "simulated"

    def __init__(self, *, failing_agents=(), qa_reject_once=(), model="claude-opus-5-5"):
        self.failing_agents = set(failing_agents)  # agent names whose "model" is down
        self.qa_reject_once = set(qa_reject_once)  # department codes QA rejects on first review
        self.qa_seen: set[str] = set()
        self.model = model
        self.calls: list[str] = []

    # -- helpers
    @staticmethod
    def _agent(system: str) -> str:
        m = re.match(r"You are (.+?), the ", system)
        return m.group(1) if m else "?"

    @staticmethod
    def _first_user(messages) -> str:
        return messages[0]["content"] if isinstance(messages[0]["content"], str) else ""

    @staticmethod
    def _tool_results(messages) -> list[dict]:
        out = []
        for m in messages:
            if m["role"] == "user" and isinstance(m["content"], list):
                for b in m["content"]:
                    if b.get("type") == "tool_result":
                        out.append(json.loads(b["content"]))
        return out

    def _text(self, text, tokens=200):
        return LLMResult(content=[{"type": "text", "text": text}], stop_reason="end_turn", input_tokens=800,
                         output_tokens=tokens, model=self.model)

    def _tools(self, calls):
        return LLMResult(content=calls, stop_reason="tool_use", input_tokens=900, output_tokens=150,
                         model=self.model, tool_calls=calls)

    # -- the "model"
    def complete(self, *, model, system, messages, tools, max_tokens):
        agent = self._agent(system)
        prompt = self._first_user(messages)
        turn = sum(1 for m in messages if m["role"] == "assistant")
        self.calls.append(agent)
        if agent in self.failing_agents:
            raise RuntimeError(f"simulated model outage for {agent}")
        if prompt.startswith("Acceptance check"):
            return self._text(f"I am {agent}; I would start by reviewing my assigned work queue.")
        if agent == "CEO" and prompt.startswith("OBJECTIVE"):
            return self._ceo_plan(prompt, messages, turn)
        if agent == "CEO" and prompt.startswith("Write a short executive summary"):
            return self._text("Objective delivered. Sales advanced the deal to negotiation; a partner brief was "
                              "prepared without client identifiers; marketing work was reassigned after an outage. "
                              "Next step: owner to review the brief and approve sending it to the partner.")
        if agent == "QA Auditor" and prompt.startswith("QA review"):
            return self._qa(prompt, turn)
        if "objective task" in prompt:
            return self._worker(agent, prompt, messages, turn)
        return self._text(f"[{agent}] done")

    def _ceo_plan(self, prompt, messages, turn):
        objective_id = re.search(rf"OBJECTIVE ({UUID})", prompt).group(1)
        deal_id = re.search(rf"Related CRM deal: ({UUID})", prompt).group(1)
        if turn == 0:  # look before acting
            return self._tools([_call(0, "agents.list", {}), _call(1, "partners.list", {})])
        results = self._tool_results(messages)
        if turn == 1:  # capability gap: nobody coordinates partners -> create a specialist
            agents = next(r for r in results if "agents" in r)["agents"]
            assert not any("partners.prepare_brief" in a["tools"] for a in agents if a["role"] != "ceo")
            return self._tools([_call(2, "agents.provision", {
                "name": "Partner Coordinator", "role": "partner_coordinator", "department_code": "sales",
                "instructions": "You coordinate work with Indian and international partner firms. You prepare "
                                "anonymised briefs and never disclose client identities.",
                "tools": ["partners.list", "partners.prepare_brief", "deals.get"], "monthly_budget_usd": 5})])
        if turn == 2:
            coordinator = next(r for r in results if "agent_id" in r and r.get("status") == "active")["agent_id"]
            partner_id = next(r for r in results if "partners" in r)["partners"][0]["id"]
            common = {"objective_id": objective_id}
            return self._tools([
                _call(3, "objectives.add_task", {**common, "department_code": "sales", "title": "Advance the deal",
                                                  "instruction": f"Log the discovery call outcome on deal {deal_id} "
                                                                 "and move it to negotiation."}),
                _call(4, "objectives.add_task", {**common, "department_code": "marketing", "title": "Case study outline",
                                                  "instruction": "Draft an outline for an anonymised case study."}),
                _call(5, "objectives.add_task", {**common, "department_code": "finance", "title": "Budget check",
                                                  "instruction": "Report current AI spend against budget with figures."}),
                _call(6, "objectives.add_task", {**common, "department_code": "sales", "title": "Partner brief",
                                                  "agent_id": coordinator,
                                                  "instruction": f"Prepare a partner brief for partner {partner_id} "
                                                                 f"on deal {deal_id}: depot network cost review."}),
            ])
        return self._text("Plan: four department tasks recorded, one new specialist created.")

    def _worker(self, agent, prompt, messages, turn):
        task_id = re.search(rf"objective task ({UUID})", prompt).group(1)
        deal = re.search(rf"deal ({UUID})", prompt)
        results = self._tool_results(messages)
        if agent == "Sales Lead" and turn == 0:
            return self._tools([
                _call(0, "crm.log_activity", {"kind": "call", "summary": "Discovery call: scope agreed in principle",
                                              "deal_id": deal.group(1)}),
                _call(1, "crm.move_deal", {"deal_id": deal.group(1), "stage": "negotiation"})])
        if agent == "Partner Coordinator" and turn == 0:
            partner = re.search(rf"partner ({UUID})", prompt).group(1)
            return self._tools([_call(0, "partners.prepare_brief", {
                "partner_id": partner, "deal_id": deal.group(1),
                "scope": "Review depot network costs for Northwind Logistics (contact maria@northwind.io, "
                         "Maria Lopez) across three sites."})])
        if agent == "Finance Controller" and turn == 0:
            return self._tools([_call(0, "budget.status", {})])
        if agent == "Finance Controller":
            spent = results[-1].get("org_spent", "?")
            figures = f" Organisation spend ${spent} of ${results[-1].get('org_budget')}." if "QA feedback" in prompt else ""
            return self._text(f"Budget reviewed for task {task_id}.{figures}")
        if results:
            return self._text(f"[{agent}] completed task {task_id}: {json.dumps(results)[:300]}")
        return self._text(f"[{agent}] completed task {task_id}: outline drafted with 4 sections.")

    def _qa(self, prompt, turn):
        task_id = re.search(rf"objective task ({UUID})", prompt).group(1)
        dept = re.search(r"\((\w+): ", prompt).group(1)
        if turn > 0:
            return self._text("Review recorded.")
        if dept in self.qa_reject_once and task_id not in self.qa_seen:
            self.qa_seen.add(task_id)
            return self._tools([_call(0, "objectives.qa_verdict", {
                "objective_task_id": task_id, "verdict": "fail", "notes": "No spend figures were reported."})])
        return self._tools([_call(0, "objectives.qa_verdict", {
            "objective_task_id": task_id, "verdict": "pass", "notes": "Result matches the instruction."})])
