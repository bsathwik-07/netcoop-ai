"""A new agent in ~20 lines: a Wi-Fi specialist that joins without touching the orchestrator.

Register it by decorator (shown) or as an entry point in pyproject.toml:
    [project.entry-points."netcoop.agents"]
    wireless_agent = "examples.custom_agent:WirelessAgent"
The correlator discovers it from its CAPABILITY_ANNOUNCE, routes it the Wi-Fi metrics, counts its
vote on weak_wifi, and can send it challenge-round QUERYs about those metrics. Its evidence is
de-duplicated against the performance agent's, so nothing is counted twice.
"""

from netcoop.agents.analysis import AnalysisAgent
from netcoop.agents.registry import register_agent
from netcoop.protocol.payloads import Skill
from netcoop.protocol.vocabulary import Hypothesis

WIFI = ("wifi_rssi_dbm", "wifi_snr_db", "retransmission_pct")


@register_agent
class WirelessAgent(AnalysisAgent):
    name = "wireless_agent"
    description = "Wi-Fi radio health: signal strength, noise and retransmissions"
    inputs = WIFI  # metrics it wants in its TASK_ASSIGNMENTs
    metrics = WIFI  # metrics it judges and answers QUERYs about
    hypotheses = (Hypothesis.WEAK_WIFI,)
    skills = (Skill(id="rf-health", name="RF health", tags=["wifi", "rf"]),)
    # Scoring, evidence, thresholds and query answers are inherited from AnalysisAgent and config/.
