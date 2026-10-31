"""The correlator's reasoning, kept free of messaging so it can be unit-tested.

Combines findings from all agents into S(h, d), explains away downstream symptoms
(Sherlock), detects conflicts, plans challenge-round queries, applies discriminating rules
per device, finds secondary incidents (SCORE-style greedy cover) and builds the Diagnosis.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import cast

from netcoop.config import Settings
from netcoop.protocol.payloads import (
    AgentFindingSummary,
    AlternativeCause,
    CapabilityAnnounce,
    ConfidenceBreakdown,
    ConflictResolution,
    DataQuality,
    Diagnosis,
    EvidenceItem,
    Finding,
    IngestionReport,
    Localization,
    Query,
    QueryPredicate,
    Response,
    SecondaryIncident,
)
from netcoop.protocol.vocabulary import HYPOTHESIS_DESCRIPTIONS, DiagnosisStatus, Hypothesis
from netcoop.scoring.confidence import (
    AgentVote,
    agreement_factor,
    decide,
    metric_coverage,
    missing_corroboration,
)
from netcoop.scoring.evidence import MetricSummary
from netcoop.scoring.hypotheses import FAULTS, eligible, profiles, softmax
from netcoop.topology import Topology


@dataclass
class PlannedQuery:
    agent: str
    query: Query
    rule_id: str | None = None


@dataclass
class Judge:
    settings: Settings
    topology: Topology | None
    findings: dict[str, Finding]
    capabilities: dict[str, CapabilityAnnounce]
    ingestion: IngestionReport
    raw: dict[tuple[Hypothesis, str], float] = field(default_factory=dict)
    observed: dict[str, dict[str, EvidenceItem]] = field(default_factory=dict)
    adjust: dict[tuple[Hypothesis, str], float] = field(default_factory=lambda: defaultdict(float))
    fired: set[tuple[str, str]] = field(default_factory=set)
    asked: set[tuple[str, str, str]] = field(default_factory=set)
    log: list[str] = field(default_factory=list)
    penalized: set[Hypothesis] = field(default_factory=set)
    queries_text: list[str] = field(default_factory=list)
    rounds: int = 0

    def __post_init__(self) -> None:
        self.profiles = profiles(self.settings)
        qf_table = self.settings.scoring["quality_factor"]
        self._qf = lambda q: float(qf_table.get(str(q), 0.0))  # noqa: E731
        for finding in self.findings.values():
            for ev in finding.evidence:
                if ev.device_id is not None:
                    self._observe(ev)
        self._recompute_raw()

    def _recompute_raw(self) -> None:
        """raw(h, d) from the de-duplicated observations (one per device and metric), so two agents that
        judge the same metric never count it twice, and confirmed challenge-round evidence counts at once.
        Agents' own hypothesis_scores stay in their FINDINGs for transparency and their local votes."""
        self.raw = {}
        for device, by_metric in self.observed.items():
            for h, prof in self.profiles.items():
                total = 0.0
                for metric, ev in by_metric.items():
                    a, b = prof.abnormal.get(metric, 0.0), prof.healthy.get(metric, 0.0)
                    if a or b:
                        total += self._qf(ev.quality) * (a * ev.severity + b * (1.0 - ev.severity))
                if total:
                    self.raw[(h, device)] = round(total, 6)

    # -- observations -------------------------------------------------------------------------

    def _observe(self, ev: EvidenceItem) -> None:
        current = self.observed.setdefault(str(ev.device_id), {}).get(ev.metric)
        if current is None or self._qf(ev.quality) >= self._qf(current.quality):
            self.observed[str(ev.device_id)][ev.metric] = ev

    def summaries(self, device: str | None) -> dict[str, MetricSummary]:
        out: dict[str, MetricSummary] = {}
        for metric, ev in self.observed.get(device or "", {}).items():
            out[metric] = MetricSummary(str(ev.device_id), metric, ev.value or 0.0, ev.unit, ev.quality,
                                        self._qf(ev.quality), ev.severity, 1)
        return out

    # -- scores ---------------------------------------------------------------------------------

    def device_score(self, h: Hypothesis, device: str) -> float:
        """S(h, d) + rule/refutation adjustments. Zero unless some abnormal evidence supports h on d:
        healthy-weight evidence may support a hypothesis but never create one on its own."""
        prof = self.profiles[h]
        obs = self.observed.get(device, {})
        supported = any(prof.abnormal.get(m, 0.0) > 0 and ev.severity > 0 and self._qf(ev.quality) > 0
                        for m, ev in obs.items())
        base = self.raw.get((h, device), 0.0) / prof.z if prof.z and supported else 0.0
        return base + self.adjust.get((h, device), 0.0)

    def devices(self) -> list[str]:
        return sorted({d for (_, d) in self.raw} | set(self.observed))

    def best_fault(self, device: str, exclude: set[Hypothesis] | None = None) -> tuple[Hypothesis | None, float]:
        best: tuple[Hypothesis | None, float] = (None, float("-inf"))
        for h in FAULTS:
            if exclude and h in exclude:
                continue
            s = self.device_score(h, device)
            if s > best[1]:
                best = (h, s)
        return best

    def explained(self) -> dict[str, str]:
        """device -> ancestor whose fault explains its (upstream) symptoms."""
        if self.topology is None:
            return {}
        threshold = float(self.settings.scoring.get("explain_away_min_score", 0.5))
        out: dict[str, str] = {}
        for device in self.devices():
            if device not in self.topology:
                continue
            for ancestor in self.topology.ancestors(device):
                h, s = self.best_fault(ancestor, exclude={Hypothesis.UPSTREAM_ISSUE})
                if h is not None and s >= threshold:
                    out[device] = ancestor
                    break
        return out

    def worst_severity(self, exclude: set[str] | None = None) -> float | None:
        """Worst severity of any usable signal (not quality-discounted, see normal_score)."""
        items = [ev for d, by in self.observed.items() if not (exclude and d in exclude)
                 for ev in by.values() if self._qf(ev.quality) > 0]
        return max(ev.severity for ev in items) if items else None

    def normal_score(self, exclude: set[str] | None = None) -> float | None:
        worst = self.worst_severity(exclude)
        if worst is None:
            return None
        return 1.0 - float(self.settings.scoring["normal"]["worst_weight"]) * worst

    def scores(self, exclude: set[str] | None = None) -> tuple[dict[Hypothesis, float], dict[Hypothesis, str | None]]:
        """Incident-level S(h) = max over devices. `exclude` removes devices that belong to a
        separate (secondary) incident, so independent faults do not compete for one root cause."""
        explained = self.explained()
        scores: dict[Hypothesis, float] = {}
        where: dict[Hypothesis, str | None] = {}
        devices = [d for d in self.devices() if not (exclude and d in exclude)]
        for h in FAULTS:
            candidates = [d for d in devices if not (h is Hypothesis.UPSTREAM_ISSUE and d in explained)]
            if not candidates:
                scores[h], where[h] = 0.0, None
                continue
            best = max(candidates, key=lambda d: (self.device_score(h, d), d))
            scores[h], where[h] = round(self.device_score(h, best), 6), best
        normal = self.normal_score(exclude)
        if normal is not None:
            scores[Hypothesis.NORMAL], where[Hypothesis.NORMAL] = round(normal, 6), None
        return scores, where

    def ranked(self) -> list[tuple[Hypothesis, float]]:
        scores, _ = self.scores()
        return sorted(scores.items(), key=lambda kv: (kv[1], kv[0].value), reverse=True)

    # -- conflict and challenge round ------------------------------------------------------------

    def conflict_reasons(self) -> list[str]:
        """Open disagreements. A dissent whose hypothesis was already penalized by a rule or a
        refutation in the challenge round counts as answered."""
        reasons: list[str] = []
        open_tops = {a: f.top_hypothesis for a, f in sorted(self.findings.items())
                     if f.top_hypothesis not in (None, Hypothesis.NORMAL)}
        fault_tops = set(open_tops.values())
        if len(fault_tops) >= 2:
            leader = self.ranked()[0][0]
            unanswered = {h for h in fault_tops if h is not leader and h not in self.penalized}
            if unanswered:
                names = ", ".join(f"{a}: {h.value}" for a, h in open_tops.items())
                reasons.append(f"agents disagree on the root cause ({names})")
        ranked = self.ranked()
        if len(ranked) >= 2 and ranked[0][0] is not Hypothesis.NORMAL:
            gap = ranked[0][1] - ranked[1][1]
            margin = float(self.settings.scoring["decision"]["conflict_margin"])
            if gap < margin:
                reasons.append(f"top hypotheses too close: {ranked[0][0].value} {ranked[0][1]:.2f} vs "
                               f"{ranked[1][0].value} {ranked[1][1]:.2f} (margin {gap:.2f} < {margin:.2f})")
        return reasons

    def _owner(self, metric: str | None = None, predicate: QueryPredicate | None = None) -> str | None:
        for agent, cap in sorted(self.capabilities.items()):
            if agent not in self.findings:
                continue
            if metric is not None and metric not in cap.metrics:
                continue
            if predicate is not None and predicate not in cap.query_predicates:
                continue
            return agent
        return None

    def plan_queries(self, incident: str) -> list[PlannedQuery]:
        """Targeted questions about the premises that separate the top two hypotheses."""
        ranked = cast(list[Hypothesis], [h for h, _ in self.ranked() if h is not Hypothesis.NORMAL][:2])
        _, where = self.scores()
        planned: list[PlannedQuery] = []
        round_no = self.rounds + 1

        def ask(agent: str | None, predicate: QueryPredicate, question: str, params: dict, rule: str | None,
                at_stake: list[Hypothesis]) -> None:
            if agent is None:
                return
            key = (agent, predicate.value, repr(sorted(params.items())))
            if key in self.asked:
                return
            self.asked.add(key)
            qid = f"{incident}-R{round_no}-Q{len(self.asked)}"
            planned.append(PlannedQuery(agent, Query(query_id=qid, round=round_no, question=question,
                                                     predicate=predicate, params=params,
                                                     hypotheses_at_stake=at_stake), rule))

        for h in ranked:
            device = where.get(h)
            if device is None:
                continue
            observed = self.observed.get(device, {})
            for rule in self.settings.weights.get("rules", []):
                if h.value not in rule["effect"] or (rule["id"], device) in self.fired:
                    continue
                if not all(m in observed for m in rule["when"]):
                    continue  # a premise is not even observed on this device: the rule cannot apply
                for metric, condition in rule["when"].items():
                    ask(self._owner(metric=metric), QueryPredicate.METRIC_ABOVE_THRESHOLD,
                        f"Is {metric} on {device} {condition}? (premise of rule {rule['id']})",
                        {"device_id": device, "metric": metric, "condition": condition}, rule["id"], ranked)
            if h is Hypothesis.UPSTREAM_ISSUE:
                ask(self._owner(predicate=QueryPredicate.FAULT_LOCALIZATION), QueryPredicate.FAULT_LOCALIZATION,
                    f"Is the fault localized above or below {device}?", {"device_id": device}, None, ranked)
            if h in (Hypothesis.DNS_FAILURE, Hypothesis.DHCP_FAILURE):
                ask(self._owner(predicate=QueryPredicate.PATH_HEALTHY), QueryPredicate.PATH_HEALTHY,
                    f"Is the IP path (latency, loss) to {device} healthy?", {"device_id": device}, None, ranked)
        self.queries_text.extend(f"{p.agent}: {p.query.question}" for p in planned)
        return planned

    def absorb(self, query: Query, response: Response | None) -> None:
        """Fold one answer (or a timeout) into the board."""
        device = str(query.params.get("device_id", ""))
        if response is None:
            self.log.append(f"no answer to '{query.question}' before the timeout")
            return
        metric = query.params.get("metric")
        if query.predicate is QueryPredicate.METRIC_ABOVE_THRESHOLD and metric:
            if response.answer is None:
                self.observed.get(device, {}).pop(str(metric), None)
            for ev in response.evidence:
                self._observe(ev)
            self._recompute_raw()
        penalty = float(self.settings.scoring.get("refute_penalty", 0.15))
        if query.predicate in (QueryPredicate.FAULT_LOCALIZATION, QueryPredicate.PATH_HEALTHY):
            for h in response.refutes:
                self.adjust[(h, device)] -= penalty
                self.penalized.add(h)
                self.log.append(f"{h.value} on {device} refuted: {response.explanation} (-{penalty:.2f})")
            if response.answer is None:
                self.log.append(f"'{query.question}' could not be answered: {response.explanation}")

    def apply_rules(self) -> None:
        """Fire every discriminating rule whose premises hold on a candidate device (once each)."""
        _, where = self.scores()
        candidates = sorted({d for d in where.values() if d})
        for rule in self.settings.weights.get("rules", []):
            for device in candidates:
                if (rule["id"], device) in self.fired or not self._premises_hold(rule["when"], device):
                    continue
                self.fired.add((rule["id"], device))
                for h_name, delta in rule["effect"].items():
                    h = Hypothesis(h_name)
                    self.adjust[(h, device)] += float(delta)
                    if delta < 0:
                        self.penalized.add(h)
                effects = ", ".join(f"{k} {v:+.2f}" for k, v in rule["effect"].items())
                self.log.append(f"rule {rule['id']} fired on {device}: {effects}")

    def _premises_hold(self, when: dict[str, str], device: str) -> bool:
        obs = self.observed.get(device, {})
        for metric, condition in when.items():
            ev = obs.get(metric)
            if ev is None or self._qf(ev.quality) == 0:
                return False
            if condition == "abnormal" and not ev.severity > 0:
                return False
            if condition == "critical" and not ev.severity >= 1.0:
                return False
            if condition == "healthy" and not ev.severity == 0:
                return False
        return True

    # -- decision -------------------------------------------------------------------------------

    def localization(self) -> Localization | None:
        for f in self.findings.values():
            if f.localization is not None and f.localization.root_device:
                return f.localization
        return None

    def diagnose(self, conflict: ConflictResolution, limitations: list[str],
                 missing_agents: list[str]) -> Diagnosis:
        scores, where = self.scores()
        if not scores or self.ingestion.fatal:
            return self._failed(conflict, limitations)
        explained = self.explained()
        # SCORE-style: faults outside the leading root's subtree are separate incidents, reported as
        # secondary and removed from the competition for the primary root cause.
        leader = max(scores.items(), key=lambda kv: (kv[1], kv[0].value))[0]
        secondary = self._secondary(leader, where.get(leader), self._symptomatic(leader, where.get(leader), explained),
                                    explained)
        if secondary:
            scores, where = self.scores(exclude={d for s in secondary for d in s.devices})
        temperature = float(self.settings.scoring["softmax_temperature"])
        probs = softmax(scores, temperature)
        ranked = sorted(probs.items(), key=lambda kv: (kv[1], kv[0].value), reverse=True)
        top = ranked[0][0]
        root = where.get(top)
        observed_root = self.summaries(root) if root else {}
        secondary_causes = {s.root_cause_code for s in secondary}

        if top is Hypothesis.NORMAL:
            coverage = round(self.ingestion.data_coverage, 4)
            missing_required = []
            worst = self.worst_severity()
            limit = float(self.settings.scoring["decision"].get("normal_max_severity", 0.5))
            if worst is not None and worst >= limit:
                # never declare "all clear" while a signal is clearly abnormal
                missing_required = [f"an explanation for abnormal signals (worst severity {worst:.2f})"]
            drifting = sorted({f"{ev.metric} on {ev.device_id}" for by in self.observed.values()
                               for ev in by.values() if ev.basis == "baseline" and ev.severity > 0})
            if drifting:
                # early warning: below every threshold, but clearly off the devices' own history
                missing_required.append(f"continued monitoring of signals drifting from their own baseline "
                                        f"(early warning): {', '.join(drifting[:4])}")
        else:
            coverage = metric_coverage(top, observed_root, self.settings)
            missing_required = missing_corroboration(top, observed_root, self.settings)
            prof = self.profiles[top]
            if not any(ev.basis == "threshold" and ev.severity > 0 and prof.abnormal.get(m, 0) > 0
                       for m, ev in self.observed.get(root or "", {}).items()):
                # baseline drift alone can raise suspicion but never support a diagnosis
                missing_required.append(f"a threshold breach on {root} (so far only drift from its own baseline): "
                                        "keep monitoring and re-run on the next window")

        votes = []
        secondary_roots = {s.devices[0] for s in secondary}
        for agent, f in sorted(self.findings.items()):
            owned = set(self.capabilities.get(agent, CapabilityAnnounce(agent=agent, version="?")).metrics)
            vote_top = f.top_hypothesis
            if vote_top is Hypothesis.UPSTREAM_ISSUE and top is not Hypothesis.UPSTREAM_ISSUE:
                # "the problem is upstream of my device" agrees with a root fault on that device's ancestor
                flagged = max((hs for hs in f.hypothesis_scores if hs.hypothesis is vote_top and hs.device_id),
                              key=lambda hs: hs.score, default=None)
                device = flagged.device_id if flagged and flagged.device_id else ""
                ancestor = explained.get(device)
                local_h, local_s = self.best_fault(device, exclude={Hypothesis.UPSTREAM_ISSUE})
                if ancestor is not None and ancestor == root:
                    vote_top = top
                elif ancestor in secondary_roots:
                    vote_top = None
                elif local_s >= float(self.settings.scoring.get("explain_away_min_score", 0.5)):
                    # neighbours are degraded, but the device has a strong local cause of its own
                    vote_top = top if local_h is top else None
            if vote_top in secondary_causes and vote_top is not top:
                vote_top = None  # this agent found the secondary incident; not a dissent on the primary
            votes.append(AgentVote(agent, vote_top, eligible(top, owned, self.settings) and bool(owned),
                                   resolved=f.top_hypothesis in self.penalized))
        agreement = agreement_factor(top, votes, self.settings)
        _, decision = decide(probs, coverage, agreement, self.settings, missing_required=missing_required,
                             fatal=self.ingestion.fatal)
        conflict.resolution = self._resolution_text(conflict, top, decision.status)
        if secondary and conflict.conflict_detected:
            conflict.resolution += ("; independent fault(s) reported as secondary incident(s): "
                                    + ", ".join(f"{s.root_cause_code.value} at {s.devices[0]}" for s in secondary))

        symptomatic = self._symptomatic(top, root, explained)
        # the same fault on several independent devices is one incident with several affected devices
        same = [s for s in secondary if s.root_cause_code is top]
        secondary = [s for s in secondary if s.root_cause_code is not top]
        co_roots = [s.devices[0] for s in same]
        if top is Hypothesis.UPSTREAM_ISSUE:
            loc = self.localization()
            if loc and loc.root_device and root in loc.affected_devices and loc.root_device != root:
                symptomatic = sorted(set(loc.affected_devices) - {loc.root_device})
                root = loc.root_device  # the shared upstream device, not one of the leaves
        segments: list[str] = []
        if root and top is not Hypothesis.NORMAL and self.topology:
            segments = sorted({z for d in [root, *co_roots, *symptomatic] if (z := self.topology.zone(d))})
        affected = ([root] if root and top is not Hypothesis.NORMAL else []) + co_roots + symptomatic

        supporting, contradicting = self._evidence_for(top, root, symptomatic)
        next_data = self._next_data(ranked, where, missing_required, decision.status, coverage)
        limitations = list(limitations)
        if missing_agents:
            limitations.append(f"no finding from: {', '.join(missing_agents)} (timed out or failed); "
                               "diagnosis based on the remaining agents")
        if decision.status is DiagnosisStatus.INSUFFICIENT_EVIDENCE:
            limitations.extend(f"abstained: {r}" for r in decision.reasons)
        if decision.status is DiagnosisStatus.DIAGNOSED and decision.category.value != "high":
            limitations.append("Root cause requires further verification")

        return Diagnosis(
            status=decision.status,
            root_cause_code=top,
            confidence=decision.confidence,
            confidence_category=decision.category,
            probabilities={h.value: round(p, 4) for h, p in ranked},
            root_device=root if top is not Hypothesis.NORMAL else None,
            affected_devices=affected,
            symptomatic_devices=symptomatic,
            affected_segments=segments,
            supporting_evidence=supporting,
            contradicting_evidence=contradicting,
            alternative_causes=self._alternatives(ranked, scores, where, top),
            agent_findings_summary=self._agent_summaries(missing_agents),
            conflict_resolution=conflict,
            data_quality=self._data_quality(),
            confidence_breakdown=ConfidenceBreakdown(
                p_top=decision.p_top, p_second=decision.p_second, margin=decision.margin,
                data_coverage=coverage, agreement_factor=agreement,
            ),
            secondary_incidents=secondary,
            next_data_to_collect=next_data,
            evidence_sources=sorted({ev.source_agent for ev in supporting + contradicting} | set(self.findings)),
            limitations=_dedupe(limitations),
        )

    def _symptomatic(self, top: Hypothesis, root: str | None, explained: dict[str, str]) -> list[str]:
        if not root or top is Hypothesis.NORMAL:
            return []
        symptomatic = {d for d, a in explained.items() if a == root and d != root}
        loc = self.localization()
        if loc and loc.root_device == root:
            symptomatic |= set(loc.symptomatic_devices)
        return sorted(symptomatic)

    def _failed(self, conflict: ConflictResolution, limitations: list[str]) -> Diagnosis:
        reasons = [r.reason for r in self.ingestion.rejections[:5]] or ["no usable telemetry"]
        return Diagnosis(
            status=DiagnosisStatus.FAILED,
            confidence=0.0,
            data_quality=self._data_quality(),
            conflict_resolution=conflict,
            limitations=_dedupe([*limitations, *(f"input rejected: {r}" for r in reasons)]),
            next_data_to_collect=["Re-send the telemetry as valid JSON/JSONL/CSV with device_id, timestamp and metrics"],
            evidence_sources=sorted(self.findings),
        )

    def _resolution_text(self, conflict: ConflictResolution, top: Hypothesis, status: DiagnosisStatus) -> str:
        if not conflict.conflict_detected:
            return "no conflict: agents' findings were consistent"
        fired = sorted({r for r, _ in self.fired})
        verb = "resolved in favour of" if status is DiagnosisStatus.DIAGNOSED else "left unresolved; leading candidate"
        detail = f" after {conflict.rounds} challenge round(s)"
        if fired:
            detail += f"; discriminating rules applied: {', '.join(fired)}"
        refuted = [line for line in self.log if "refuted" in line]
        if refuted:
            detail += f"; {len(refuted)} hypothesis refutation(s) from topology/path checks"
        return f"{verb} {top.value}{detail}"

    def _evidence_for(self, top: Hypothesis, root: str | None, symptomatic: list[str]
                      ) -> tuple[list[EvidenceItem], list[EvidenceItem]]:
        if top is Hypothesis.NORMAL or root is None:
            worst = sorted((ev for by in self.observed.values() for ev in by.values()),
                           key=lambda e: (-e.severity, e.device_id or "", e.metric))
            return worst[:5], [ev for ev in worst if ev.severity > 0][:5]
        prof = self.profiles[top]
        obs = self.observed.get(root, {})
        support = [ev for m, ev in obs.items() if prof.abnormal.get(m, 0) > 0 and ev.severity > 0]
        support.sort(key=lambda e: -prof.abnormal.get(e.metric, 0) * e.severity)
        against = [ev for m, ev in obs.items()
                   if (prof.abnormal.get(m, 0) < 0 and ev.severity > 0)
                   or (prof.abnormal.get(m, 0) >= 0.6 and ev.severity == 0)]
        downstream: list[EvidenceItem] = []
        for device in symptomatic[:3]:
            items = [ev for ev in self.observed.get(device, {}).values() if ev.severity > 0]
            if items:
                strongest = max(items, key=lambda e: e.severity)
                downstream.append(strongest.model_copy(update={"interpretation": strongest.interpretation
                                                               + f" (symptom downstream of {root})"}))
        return (support[:8] + downstream), against[:8]

    def _alternatives(self, ranked: list[tuple[Hypothesis, float]], scores: dict[Hypothesis, float],
                      where: dict[Hypothesis, str | None], top: Hypothesis) -> list[AlternativeCause]:
        out: list[AlternativeCause] = []
        for h, p in ranked[1:5]:
            if p < 0.001 and len(out) >= 2:
                break
            reasons = [f"score {scores[h]:.2f} vs {scores[top]:.2f} for {top.value}"]
            device = where.get(h)
            penalties = [line.split(":")[0].replace("rule ", "") for line in self.log
                         if line.startswith("rule ") and f" {h.value} -" in line]
            if penalties:
                reasons.append(f"contradicted by {', '.join(sorted(set(penalties)))}")
            if device and h is not Hypothesis.NORMAL:
                prof = self.profiles[h]
                obs = self.observed.get(device, {})
                healthy_key = [m for m, w in sorted(prof.abnormal.items(), key=lambda kv: -kv[1])
                               if w >= 0.6 and m in obs and obs[m].severity == 0]
                missing_key = [m for m, w in sorted(prof.abnormal.items(), key=lambda kv: -kv[1])
                               if w >= 0.6 and m not in obs]
                if healthy_key:
                    reasons.append(f"expected signals are healthy on {device}: {', '.join(healthy_key[:3])}")
                if missing_key:
                    reasons.append(f"key signals not observed: {', '.join(missing_key[:3])}")
            out.append(AlternativeCause(cause=h, description=HYPOTHESIS_DESCRIPTIONS[h], probability=round(p, 4),
                                        why_less_likely="; ".join(reasons)))
        return out

    def _secondary(self, top: Hypothesis, root: str | None, symptomatic: list[str],
                   explained: dict[str, str]) -> list[SecondaryIncident]:
        if top is Hypothesis.NORMAL or root is None:
            return []
        threshold = float(self.settings.scoring.get("secondary_min_score", 0.6))
        covered = {root, *symptomatic}
        if self.topology and root in self.topology:
            covered |= self.topology.descendants(root)
        found: list[SecondaryIncident] = []
        candidates = []
        for device in self.devices():
            if device in covered or device in explained:
                continue
            h, s = self.best_fault(device)
            if h is not None and s >= threshold:
                candidates.append((s, device, h))
        for s, device, h in sorted(candidates, reverse=True):  # greedy: strongest unexplained first
            if device in covered:
                continue
            members = [device]
            if self.topology and device in self.topology:
                members += sorted(d for d in self.topology.descendants(device) if d in self.observed)
            covered |= set(members)
            found.append(SecondaryIncident(root_cause_code=h, devices=members, probability=round(min(s, 1.0), 4),
                                           explanation=f"{HYPOTHESIS_DESCRIPTIONS[h]} at {device}, not explained by "
                                                       f"the primary fault at {root}"))
        return found[:3]

    def _next_data(self, ranked: list[tuple[Hypothesis, float]], where: dict[Hypothesis, str | None],
                   missing_required: list[str], status: DiagnosisStatus, coverage: float) -> list[str]:
        if status is DiagnosisStatus.NORMAL or (status is DiagnosisStatus.DIAGNOSED and coverage >= 0.9):
            return []
        out: list[str] = []
        top_device = where.get(ranked[0][0]) or next((d for d in where.values() if d), None)
        for metric in missing_required:
            if metric == "shared_upstream_symptoms":
                out.append(f"telemetry from devices sharing the upstream of {top_device}, to tell a local fault "
                           f"from an upstream one")
            else:
                out.append(metric)  # already a readable request (see diagnose)
        for h, _p in ranked[:3]:
            if h is Hypothesis.NORMAL:
                continue
            device = where.get(h) or top_device
            obs = self.observed.get(device or "", {})
            for metric, w in sorted(self.profiles[h].abnormal.items(), key=lambda kv: -kv[1]):
                if w >= 0.6 and metric not in obs and not metric.startswith(("device_status", "uptime_reset")):
                    text = f"{metric} on {device} (would confirm or rule out {h.value})"
                    if metric == "shared_upstream_symptoms":
                        text = f"telemetry from devices sharing the upstream of {device} (confirms/rules out {h.value})"
                    out.append(text)
        return _dedupe(out)[:6]

    def _agent_summaries(self, missing: list[str]) -> list[AgentFindingSummary]:
        out = []
        for agent, f in sorted(self.findings.items()):
            best = max((hs.score for hs in f.hypothesis_scores if hs.hypothesis is f.top_hypothesis), default=0.0)
            out.append(AgentFindingSummary(agent=agent, top_hypothesis=f.top_hypothesis, score=round(best, 4),
                                           evidence_count=len(f.evidence)))
        out.extend(AgentFindingSummary(agent=a, responded=False) for a in missing)
        return out

    def _data_quality(self) -> DataQuality:
        rep = self.ingestion
        warnings: list[str] = []  # error-code summaries first, so they are never truncated away
        by_code: dict[str, list[str]] = defaultdict(list)
        for note in rep.rejections:
            by_code[note.code.value].append(note.reason)
        for code, reasons in sorted(by_code.items()):
            more = f" (+{len(reasons) - 1} similar)" if len(reasons) > 1 else ""
            warnings.append(f"{code}: {reasons[0]}{more}")
        warnings.extend(rep.warnings)
        return DataQuality(coverage=rep.data_coverage, records_in=rep.records_in,
                           conversions=sum(c.count for c in rep.conversions), rejected_records=rep.records_rejected,
                           unmapped_fields=rep.unmapped_fields, warnings=warnings[:20])


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
